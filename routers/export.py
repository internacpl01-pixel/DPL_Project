"""Export router — CSV, Excel (.xlsx), and PDF download of master table data."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse

from fpdf import FPDF

from dependencies import get_current_user
from services.data import get_master_rows

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["export"])

_NUMERIC_TYPES = {"numeric", "real", "double precision", "integer", "bigint"}


# ── helpers ──────────────────────────────────────────────────────────

def _is_numeric(col_type: str) -> bool:
    return (col_type or "").lower() in _NUMERIC_TYPES


async def _fetch_all(search: str = "") -> tuple[list[dict], list[dict]]:
    """Return (columns, rows) with the full master table.

    Columns come from the first page; rows are paginated in 500-row chunks.
    Client-side search filtering is applied when *search* is non-empty.
    """
    limit = 500
    offset = 0
    all_rows: list[dict] = []
    columns: list[dict] = []

    while True:
        result = await get_master_rows(limit=limit, offset=offset)
        if not columns and result.get("columns"):
            columns = result["columns"]
        batch = result.get("rows", [])

        if search:
            term = search.lower()
            batch = [
                r for r in batch
                if any(str(v).lower().find(term) != -1 for v in r.values() if v is not None)
            ]

        all_rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    return columns, all_rows


# ── CSV ──────────────────────────────────────────────────────────────

def _build_csv(
    col_names: list[str], col_display: list[str], col_types: dict[str, str], rows: list[dict]
) -> tuple[bytes, str, str]:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)

    writer.writerow(col_display)

    for row in rows:
        out: list[str] = []
        for c in col_names:
            val = row.get(c, "")
            if val is None:
                val = ""
            elif _is_numeric(col_types.get(c, "")):
                val = str(val)
            else:
                val = str(val)
            out.append(val)
        writer.writerow(out)

    return buf.getvalue().encode("utf-8-sig"), "text/csv", "csv"


# ── Excel ────────────────────────────────────────────────────────────

def _build_xlsx(
    col_names: list[str], col_display: list[str], col_types: dict[str, str], rows: list[dict]
) -> tuple[bytes, str, str]:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(status_code=500, detail="Excel export requires openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "Master Data"

    _HDR_FILL = PatternFill(fill_type="solid", fgColor="DDDDDD")

    # header row
    for ci, label in enumerate(col_display, 1):
        cell = ws.cell(row=1, column=ci, value=label)
        cell.font = cell.font.copy(bold=True)
        cell.fill = _HDR_FILL

    # data rows
    for ri, row in enumerate(rows, 2):
        for ci, c in enumerate(col_names, 1):
            val = row.get(c, "")
            cell = ws.cell(row=ri, column=ci)
            if val is None or val == "":
                cell.value = None
            elif _is_numeric(col_types.get(c, "")):
                try:
                    cell.value = float(str(val).replace(",", ""))
                    cell.number_format = "#,##0.00"
                except (ValueError, TypeError):
                    cell.value = str(val)
            else:
                cell.value = str(val)

    # auto-fit column widths (cap at 50 chars)
    for ci in range(1, len(col_names) + 1):
        max_len = len(col_display[ci - 1])
        for ri in range(2, min(len(rows) + 2, 200)):
            v = ws.cell(row=ri, column=ci).value
            if v is not None:
                max_len = max(max_len, min(len(str(v)), 50))
        ws.column_dimensions[get_column_letter(ci)].width = min(max_len + 2, 50)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"


# ── PDF ──────────────────────────────────────────────────────────────

class _ExportPDF(FPDF):
    """Table-based PDF export — fixed row heights, no wrapping chaos."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._title_rows = 0

    def header(self) -> None:
        if self.page_no() == 1:
            self._title_rows = 0  # first page uses regular header
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 7, "Master Data Export", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5,
            f"Generated: {date.today().isoformat()}     "
            f"Rows: {self.row_count}",
            new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self._title_rows += 2 if self.page_no() == 1 else 1

    def footer(self) -> None:
        self.set_y(-10)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _build_pdf(
    col_names: list[str], col_display: list[str], col_types: dict[str, str], rows: list[dict]
) -> tuple[bytes, str, str]:
    pdf = _ExportPDF(orientation="L", unit="mm", format="A4")
    pdf.row_count = len(rows)
    pdf.set_auto_page_break(auto=True, margin=10)

    usable_w = pdf.w - pdf.l_margin - pdf.r_margin  # landscape A4 ≈ 277 mm
    n = len(col_names)

    # Fixed column widths (mm) — tuned for 7-column bank statement layout
    _WIDTHS = {
        "id":          12,
        "date":        22,
        "desc":       140,
        "withdrawal":  25,
        "deposits":    25,
        "balance":     25,
        "account_num": 30,
    }
    # Fallback width for unknown columns
    def _col_w(name: str) -> float:
        return _WIDTHS.get(name, usable_w / max(n, 1))

    col_w = [_col_w(c) for c in col_names]
    row_h = 5.5
    total_w = sum(col_w)

    def _cell(text: str, w: float, bold: bool = False, align: str = "L") -> None:
        if bold:
            pdf.set_font("Helvetica", "B", 7)
        else:
            pdf.set_font("Helvetica", "", 7)
        pdf.cell(w, row_h, text if len(text) < 80 else text[:77] + "...",
                 border=1, align=align)

    def _header_row() -> None:
        pdf.set_fill_color(230, 230, 230)
        for label, w in zip(col_display, col_w):
            pdf.cell(w, row_h, label, border=1, align="L", fill=True)
        pdf.ln(row_h)

    def _data_row(row: dict) -> None:
        for c, w in zip(col_names, col_w):
            val = row.get(c, "")
            if val is None:
                val = ""
            val = str(val)
            if _is_numeric(col_types.get(c, "")):
                _cell(val, w, align="R")
            else:
                _cell(val, w)

    if n == 0 or not rows:
        pdf.add_page()
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, "No data to export.", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.add_page()
        _header_row()
        for row in rows:
            if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                _header_row()
            _data_row(row)
            pdf.ln(row_h)

    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue(), "application/pdf", "pdf"


# ── endpoint ─────────────────────────────────────────────────────────

@router.get("/export")
async def export_data(
    format: str = Query("csv", pattern="^(csv|xlsx|pdf)$"),
    search: str = Query("", description="Filter rows by text (matches any column)"),
    current_user: dict = Depends(get_current_user),
):
    columns, rows = await _fetch_all(search=search)

    if not columns:
        columns = [{"name": "id", "displayname": "ID", "type": "integer"}]

    col_names = [c["name"] for c in columns]
    col_display = [c.get("displayname") or c["name"] for c in columns]
    col_types = {c["name"]: c.get("type", "") for c in columns}

    today_str = date.today().isoformat()
    filename = f"master_data_{today_str}"

    try:
        if format == "csv":
            content, media_type, ext = _build_csv(col_names, col_display, col_types, rows)
        elif format == "xlsx":
            content, media_type, ext = _build_xlsx(col_names, col_display, col_types, rows)
        else:
            content, media_type, ext = _build_pdf(col_names, col_display, col_types, rows)
    except Exception as exc:
        logger.error(f"Export failed ({format}): {exc}")
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.{ext}"',
        },
    )
