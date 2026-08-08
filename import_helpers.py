"""
Import helpers for bank statement PDF processing.
Handles field resolution via the existing fieldmap table,
and appending rows to the master table.
"""

import logging
import re
import io

logger = logging.getLogger(__name__)


def resolve_field_map(fieldmap_rows: list) -> dict:
    """
    Build a lookup dict from the fieldmap table.
    Maps each alias (lowercased, stripped) to its canonical fieldname.

    Returns:
        {"date": "date", "transaction date": "date", "narration": "desc", ...}
    """
    alias_map = {}
    for row in fieldmap_rows:
        fieldname = row.get("fieldname", "")
        mapfields = row.get("mapfields", "")
        if not mapfields:
            continue
        for alias in mapfields.split(","):
            alias = alias.strip().lower()
            if alias:
                alias_map[alias] = fieldname
    return alias_map


def resolve_column(header_name: str, alias_map: dict) -> str:
    """
    Map a PDF column header to a canonical master fieldname using fieldmap aliases.
    Case-insensitive, whitespace-tolerant.

    Returns the canonical fieldname, or the original header lowercased if no match.
    """
    cleaned = header_name.strip().lower()
    # Direct match
    if cleaned in alias_map:
        return alias_map[cleaned]
    # Normalize whitespace and punctuation
    normalized = re.sub(r"[^\w\s]", "", cleaned).strip()
    if normalized in alias_map:
        return alias_map[normalized]
    # Return the cleaned header as-is (unknown field)
    return cleaned


# Canonical master column groups
_DATE_COLS = {"date", "value_date", "entry_date", "tran_date", "txn_date"}
_DESC_COLS = {"desc", "description", "particulars", "narration", "remarks"}
_WITHDR_COLS = {"withdrawal", "debit", "dr", "amount_out"}
_DEPOSIT_COLS = {"deposits", "deposit", "credit", "cr", "amount_in", "deposit amt"}
_BALANCE_COLS = {"balance", "closing_balance", "balance (rs.)", "available_balance"}
_REF_COLS = {"reference_no", "ref_no", "chq_ref_no", "cheque_no", "reference", "instrument_no", "ref"}


def _categorize_field(fieldname: str) -> str:
    """Return the canonical master category for a fieldname."""
    if fieldname in _DATE_COLS:
        return "date"
    if fieldname in _DESC_COLS:
        return "description"
    if fieldname in _WITHDR_COLS:
        return "withdrawal"
    if fieldname in _DEPOSIT_COLS:
        return "deposits"
    if fieldname in _BALANCE_COLS:
        return "balance"
    if fieldname in _REF_COLS:
        return "reference_no"
    return fieldname  # custom field — use as-is


def normalize_headers(extracted_headers: list, alias_map: dict) -> dict:
    """
    Map PDF column headers to canonical master field names.

    Args:
        extracted_headers: list of raw header strings from the PDF parser
        alias_map: output from resolve_field_map()

    Returns:
        {"date": "date", "narration": "desc", "withdrawal": "withdrawal", ...}
        Keys are canonical master fieldnames.
    """
    resolved = {}
    for header in extracted_headers:
        canonical = resolve_column(header, alias_map)
        category = _categorize_field(canonical)
        resolved[category] = canonical
    return resolved


def build_master_insert(fields_map: dict, row_data: dict) -> tuple:
    """
    Build a column/value tuple for inserting into the master table.

    Args:
        fields_map: output from normalize_headers() — maps canonical field to source col
        row_data: raw row data from parser

    Returns:
        (column_names_str, placeholders_str, values_tuple)
    """
    columns = []
    values = []

    # Known canonical columns in master table
    canonical_cols = [
        "date", "desc", "withdrawal", "deposits", "balance",
        "field_date_1", "field_date_2", "field_date_3",
        "field_date_4", "field_date_5",
        "field_num_1", "field_num_2", "field_num_3", "field_num_4",
        "field_num_5", "field_num_6", "field_num_7", "field_num_8",
        "field_num_9", "field_num_10",
        "field_text_1", "field_text_2", "field_text_3", "field_text_4",
        "field_text_5", "field_text_6", "field_text_7", "field_text_8",
        "field_text_9", "field_text_10", "field_text_11", "field_text_12",
        "field_text_13", "field_text_14", "field_text_15", "field_text_16",
        "field_text_17", "field_text_18", "field_text_19", "field_text_20",
    ]

    used_fields = set()

    for canon in canonical_cols:
        if canon not in fields_map.values():
            continue
        # Find which source field maps to this canonical
        source_key = None
        for k, v in fields_map.items():
            if v == canon:
                source_key = k
                break

        if source_key and source_key in row_data:
            columns.append(canon)
            values.append(row_data[source_key] if row_data[source_key] else None)
            used_fields.add(source_key)

    # If nothing matched at all, insert a minimal row with just date
    if not columns:
        columns.append("date")
        values.append(row_data.get("date") or None)

    cols_str = ", ".join(f'"{c}"' if c == "desc" else c for c in columns)
    placeholders = ", ".join(["%s"] * len(values))
    return cols_str, placeholders, tuple(values)


def append_rows_to_master(conn, rows: list, fieldmap_rows: list) -> int:
    """
    Append normalized rows to the master table.
    No duplicate checks, no upsert, no delete-before-import.

    Args:
        conn: open psycopg2 connection
        rows: list of normalized row dicts from the parser
        fieldmap_rows: list of fieldmap dicts from the database

    Returns:
        Number of rows successfully inserted.
    """
    if not rows:
        return 0

    alias_map = resolve_field_map(fieldmap_rows)

    # Build field mapping from row keys
    # The parser always returns: date, description, withdrawal, deposits, balance, reference_no
    parser_keys_to_canonical = {
        "date": "date",
        "description": "desc",
        "withdrawal": "withdrawal",
        "deposits": "deposits",
        "balance": "balance",
        "reference_no": "reference_no",
    }

    # Resolve each canonical through fieldmap (in case the parser key matches an alias)
    resolved_fields = {}
    for parser_key, canon in parser_keys_to_canonical.items():
        # Check if the parser key itself matches a fieldmap alias
        fieldname = resolve_column(parser_key, alias_map)
        if fieldname == parser_key or fieldname in {
            "date", "desc", "withdrawal", "deposits", "balance", "reference_no"
        }:
            resolved_fields[parser_key] = canon
        else:
            resolved_fields[parser_key] = fieldname

    cursor = conn.cursor()
    inserted = 0

    for row in rows:
        columns = []
        values = []

        for parser_key, master_col in resolved_fields.items():
            val = row.get(parser_key, "")
            # Only include if the column actually exists in master
            if master_col and val:
                columns.append(master_col)
                values.append(val)

        if not columns:
            continue

        cols_str = ", ".join(
            f'"{c}"' if c == 'desc' else c for c in columns
        )
        placeholders = ", ".join(["%s"] * len(values))

        try:
            cursor.execute(
                f"INSERT INTO master ({cols_str}) VALUES ({placeholders})",
                tuple(values),
            )
            inserted += 1
        except Exception as e:
            logger.warning(f"Failed to insert row: {row}, error: {e}")
            # Skip this row, continue with others

    conn.commit()
    cursor.close()

    logger.info(f"Inserted {inserted}/{len(rows)} rows into master")
    return inserted
