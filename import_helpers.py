"""
Import helpers for bank statement PDF processing.
Handles field resolution via the existing fieldmap table,
and appending rows to the master table using type-aware normalization.
"""

import logging
import re
from datetime import date as _date

logger = logging.getLogger(__name__)


def _parse_date_to_date(val) -> _date | None:
    """Parse various date string formats into a datetime.date object."""
    s = str(val).strip() if val else ""
    if not s:
        return None
    # YYYY-MM-DD
    if len(s) == 10 and s[4] == "-":
        try:
            return _date.fromisoformat(s)
        except ValueError:
            pass
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", s)
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    # DD-Mon-YYYY
    m = re.match(r"(\d{1,2})-([A-Za-z]{3,})-(\d{2,4})", s)
    if m:
        month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                     "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        mon = month_map.get(m.group(2).lower()[:3])
        if mon:
            try:
                year = m.group(3)
                if len(year) == 2:
                    year = "20" + year
                return _date(int(year), mon, int(m.group(1)))
            except ValueError:
                pass
    return None


def resolve_field_map(fieldmap_rows: list) -> dict:
    """Build {normalized_alias: fieldname} from fieldmap rows."""
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


async def append_rows_to_master(conn, rows: list, fieldmap_rows: list) -> int:
    """
    Insert parsed rows into master table using fieldmap for column mapping.
    Type-aware: dates → DATE, numeric columns → REAL, rest → TEXT.
    Chunks inserts to stay under asyncpg's 32,767 parameter limit.
    """
    if not rows:
        return 0

    alias_map = resolve_field_map(fieldmap_rows)

    # Get live columns and their types from information_schema
    live_col_rows = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'master' ORDER BY ordinal_position"
    )
    live_cols = {r["column_name"]: r["data_type"] for r in live_col_rows}

    # Canonical parser keys → how to resolve them to master columns
    canonical_keys = {
        "date":      {"category": "date"},
        "description": {"category": "description"},
        "withdrawal":  {"category": "withdrawal"},
        "deposits":    {"category": "deposits"},
        "balance":     {"category": "balance"},
        "reference_no": {"category": "reference"},
    }

    # For each canonical key, find the best matching master column
    key_to_master = {}
    for ck in canonical_keys:
        for alias, fieldname in alias_map.items():
            if canonical_keys[ck]["category"] == fieldname or ck == fieldname:
                if fieldname in live_cols:
                    key_to_master[ck] = fieldname
                    break

    if not key_to_master:
        # No fieldmap configured yet — use defaults
        key_to_master = {
            "date": "date",
            "description": "desc",
            "withdrawal": "withdrawal",
            "deposits": "deposits",
            "balance": "balance",
        }
        # Only keep columns that actually exist
        key_to_master = {k: v for k, v in key_to_master.items() if v in live_cols}

    cols_list = sorted(key_to_master.values())
    col_indices = {c: i for i, c in enumerate(cols_list)}

    # Build flat values array, coercing types per column
    flat_values = []
    for row in rows:
        row_vals = [None] * len(cols_list)
        for parser_key, master_col in key_to_master.items():
            val = row.get(parser_key, "")
            if val is None or val == "":
                continue

            col_type = live_cols.get(master_col, "text")

            if col_type in ("date", "timestamp without time zone", "timestamp"):
                d = _parse_date_to_date(val)
                if d is None:
                    continue
                row_vals[col_indices[master_col]] = d
            elif col_type in ("real", "double precision", "numeric"):
                try:
                    row_vals[col_indices[master_col]] = float(str(val).replace(",", ""))
                except (ValueError, TypeError):
                    continue
            else:
                # TEXT or other — keep as string
                row_vals[col_indices[master_col]] = str(val).strip()

        if any(v is not None for v in row_vals):
            flat_values.extend(row_vals)

    if not flat_values:
        return 0

    # Chunked INSERT to stay under asyncpg's 32,767 parameter limit
    CHUNK_SIZE = 200  # rows per chunk
    cols_str = ", ".join(f'"{c}"' if c == 'desc' else c for c in cols_list)

    total_inserted = 0
    for chunk_start in range(0, len(flat_values), len(cols_list) * CHUNK_SIZE):
        chunk_end = chunk_start + len(cols_list) * CHUNK_SIZE
        chunk_vals = flat_values[chunk_start:chunk_end]
        num_rows = len(chunk_vals) // len(cols_list)

        placeholders = []
        p = 1
        for _ in range(num_rows):
            placeholders.append("(" + ", ".join(f"${p + i}" for i in range(len(cols_list))) + ")")
            p += len(cols_list)

        sql = f"INSERT INTO master ({cols_str}) VALUES {', '.join(placeholders)}"
        await conn.execute(sql, *chunk_vals)
        total_inserted += num_rows

    return total_inserted
