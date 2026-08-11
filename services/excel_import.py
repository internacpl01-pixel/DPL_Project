"""Excel import service — reads .xlsx/.xls bank statements and appends to master.

Uses the same fieldmap + live-column-types pipeline as the PDF importer.
Header detection and row assembly are shared with parsers.py, so column
mapping, row boundaries, type coercion, and chunked INSERTs behave
identically for PDF and Excel.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from io import BytesIO

import openpyxl

from services.mappings import get_field_mappings
from database import Database
from parsers import (_build_alias_map, _detect_header_row, _assemble_rows,
                     _extract_document_level_fields)

logger = logging.getLogger(__name__)


def _read_excel_rows(file_bytes: bytes) -> list:
    """Read all rows from an Excel file (first sheet only).

    Date/datetime cells are converted to ISO date strings so date detection
    and type coercion work exactly as they do for PDF-extracted text
    (str(datetime) would otherwise yield "2026-08-04 00:00:00", which the
    date detectors reject).
    """
    wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = []
        for c in row:
            if c is None:
                cells.append("")
            elif isinstance(c, (datetime, date)):
                cells.append(c.strftime("%Y-%m-%d"))
            else:
                cells.append(str(c).strip())
        if any(cells):  # skip completely empty rows
            rows.append(cells)
    wb.close()
    return rows


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
        return {"rows": [], "row_count": 0, "inserted": 0, "headers_detected": {}, "unmapped_headers": [], "stats": {}}

    header_idx, col_mapping = _detect_header_row(rows, alias_map)

    if header_idx < 0 or not col_mapping:
        return {
            "rows": [],
            "row_count": 0,
            "inserted": 0,
            "headers_detected": {},
            "unmapped_headers": [],
            "error": "Could not detect a header row. Ensure column headers match your fieldmap aliases.",
            "stats": {},
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

    assembled, carry = _assemble_rows(rows, header_idx, col_mapping, live_col_types)
    if carry:
        assembled.append(carry)

    # Document-level fields: custom fields whose value sits above the table
    # (e.g. "Account Number 045563200000264" in the sheet's header rows)
    doc_fields = {}
    if assembled and header_idx > 0:
        header_text = "\n".join(" ".join(r) for r in rows[:header_idx])
        filled_keys = set()
        for r in assembled:
            filled_keys.update(r.keys())
        doc_fields = _extract_document_level_fields(header_text, fieldmap_rows, filled_keys)
        for r in assembled:
            for fn, val in doc_fields.items():
                r.setdefault(fn, val)

    # Per-field fill rates
    from import_helpers import compute_fill_rates
    fill_rates = compute_fill_rates(assembled)

    inserted_count = 0
    if save and assembled:
        async with Database.acquire() as conn:
            from import_helpers import append_rows_to_master
            inserted_count = await append_rows_to_master(conn, assembled, fieldmap_rows, live_cols=live_col_types)

    t_total = (time.perf_counter() - t_start) * 1000
    logger.info(f"[Excel] TOTAL: {t_total:.0f}ms, rows={len(assembled)}, inserted={inserted_count}")

    return {
        "rows": assembled,
        "row_count": len(assembled),
        "inserted": inserted_count,
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "document_fields": doc_fields,
        "stats": {"dates_in_raw_text": 0},
        "fill_rates": fill_rates,
    }
