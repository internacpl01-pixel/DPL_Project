"""Excel import service — reads .xlsx/.xls bank statements and appends to master.

Uses the same fieldmap + live-column-types pipeline as the PDF importer,
so column mapping, type coercion, and chunked INSERTs are identical.
"""
from __future__ import annotations

import logging
import re
import time
import openpyxl

from services.mappings import get_field_mappings
from database import Database
from parsers import _build_alias_map, _fieldname_category, _parse_date

logger = logging.getLogger(__name__)


def _read_excel_rows(file_bytes: bytes) -> list:
    """Read all rows from an Excel file (first sheet only)."""
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if any(cells):  # skip completely empty rows
            rows.append(cells)
    wb.close()
    return rows


def _match_alias(header_text: str, alias_map: dict):
    """Try to match a header cell against the alias map.
    Returns (matched_alias, fieldname) or (None, None).
    """
    cleaned = header_text.strip().lower()
    if cleaned in alias_map:
        return cleaned, alias_map[cleaned]
    normalized = re.sub(r"[^\w\s]", "", cleaned).strip()
    if normalized in alias_map:
        return normalized, alias_map[normalized]
    return None, None


def _detect_header_row(rows: list, alias_map: dict) -> tuple:
    """Find the header row by matching >=2 cells against fieldmap aliases.
    Returns (header_index, {col_idx: fieldname}).
    """
    for idx, row in enumerate(rows):
        mapping = {}
        for col_idx, cell in enumerate(row):
            if not cell:
                continue
            matched = _match_alias(cell, alias_map)
            if matched[0]:
                mapping[col_idx] = matched[1]
        if len(mapping) >= 2:
            return idx, mapping
    return -1, {}


def _looks_like_date(val) -> bool:
    """Quick check if a cell value looks like a date."""
    s = str(val).strip()
    if not s:
        return False
    date_patterns = [
        re.compile(r"^\d{4}-\d{2}-\d{2}$"),
        re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
        re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$"),
        re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$"),
    ]
    for pat in date_patterns:
        if pat.match(s):
            return True
    return False


def _assemble_excel_rows(rows: list, header_idx: int, col_mapping: dict,
                         live_col_types: dict) -> list:
    """Assemble transaction rows from Excel table data.
    Row keys = fieldmap fieldnames. Column roles come from information_schema types.
    """
    date_col = None
    text_cols = set()
    numeric_cols = set()

    for col_idx, fieldname in col_mapping.items():
        col_type = (live_col_types.get(fieldname) or "").lower()
        if col_type in ("date", "timestamp without time zone", "timestamp"):
            date_col = col_idx
        elif col_type in ("text", "character varying", "varchar"):
            text_cols.add(col_idx)
        elif col_type in ("real", "double precision", "numeric", "integer", "bigint"):
            numeric_cols.add(col_idx)

    if date_col is None:
        for col_idx, fieldname in col_mapping.items():
            fn_lower = fieldname.lower()
            if fn_lower in ("date", "value_date", "entry_date", "tran_date", "txn_date"):
                date_col = col_idx
                break

    _FOOTER_KEYWORDS = {
        "total", "closing balance", "b/f", "c/f", "b/fwd", "c/fwd",
        "opening balance", "summary", "grand total", "page",
    }

    result = []
    current_row = None

    for row_idx in range(header_idx + 1, len(rows)):
        row_cells = rows[row_idx]
        if not row_cells:
            continue

        # Pad to same length as header
        while len(row_cells) < len(rows[header_idx]):
            row_cells.append("")

        is_footer = False
        for col_idx in text_cols:
            if col_idx < len(row_cells):
                cell_lower = row_cells[col_idx].lower()
                for kw in _FOOTER_KEYWORDS:
                    if kw == cell_lower or cell_lower.startswith(kw):
                        is_footer = True
                        break
            if is_footer:
                break
        if is_footer:
            if current_row and current_row.get("date"):
                result.append(current_row)
                current_row = None
            continue

        if date_col is not None and _looks_like_date(row_cells[date_col]):
            if current_row and current_row.get("date"):
                result.append(current_row)

            current_row = {}
            for col_idx, cell in enumerate(row_cells):
                fieldname = col_mapping.get(col_idx)
                if not fieldname or not cell:
                    continue
                current_row[fieldname] = cell

        elif current_row and current_row.get("date"):
            for col_idx in text_cols:
                if col_idx < len(row_cells) and row_cells[col_idx]:
                    fieldname = col_mapping.get(col_idx)
                    if fieldname:
                        if fieldname in current_row and current_row[fieldname]:
                            current_row[fieldname] += " " + row_cells[col_idx]
                        else:
                            current_row[fieldname] = row_cells[col_idx]

    if current_row and current_row.get("date"):
        result.append(current_row)

    return result


async def process_excel_import(file_bytes: bytes, save: bool = False):
    """Main entry point: parse Excel file and optionally save to master."""
    t_start = time.perf_counter()

    fieldmap_rows = await get_field_mappings()
    alias_map = _build_alias_map(fieldmap_rows)

    live_col_types = {}
    async with Database.acquire() as conn:
        col_rows = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'master' ORDER BY ordinal_position"
        )
        live_col_types = {r["column_name"]: r["data_type"] for r in col_rows}

    logger.info(f"[Excel] fieldmap: {len(fieldmap_rows)}, live columns: {len(live_col_types)}")

    try:
        rows = _read_excel_rows(file_bytes)
    except Exception as e:
        raise RuntimeError(f"Failed to read Excel file: {e}")

    if not rows:
        return {"rows": [], "row_count": 0, "inserted": 0, "headers_detected": {}, "unmapped_headers": []}

    header_idx, col_mapping = _detect_header_row(rows, alias_map)

    if header_idx < 0:
        return {
            "rows": [],
            "row_count": 0,
            "inserted": 0,
            "headers_detected": {},
            "unmapped_headers": [],
            "error": "Could not detect a header row. Ensure column headers match your fieldmap aliases.",
        }

    headers_detected = {}
    unmapped_headers = []
    for col_idx, fieldname in col_mapping.items():
        if col_idx < len(rows[header_idx]):
            headers_detected[fieldname] = rows[header_idx][col_idx]
    for col_idx, cell in enumerate(rows[header_idx]):
        cell_str = str(cell).strip() if cell else ""
        if cell_str and col_idx not in col_mapping:
            unmapped_headers.append(cell_str)

    assembled = _assemble_excel_rows(rows, header_idx, col_mapping, live_col_types)

    inserted_count = 0
    if save and assembled:
        async with Database.acquire() as conn:
            from import_helpers import append_rows_to_master
            inserted_count = await append_rows_to_master(conn, assembled, fieldmap_rows)

    t_total = (time.perf_counter() - t_start) * 1000
    logger.info(f"[Excel] TOTAL: {t_total:.0f}ms, rows={len(assembled)}, inserted={inserted_count}")

    return {
        "rows": assembled,
        "row_count": len(assembled),
        "inserted": inserted_count,
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
    }