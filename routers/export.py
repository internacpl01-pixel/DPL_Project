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
    """Minimal PDF with a header, data table, and footer."""

    def header(self) -> None:
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 8, "Master Data Export", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, f"Generated: {date.today().isoformat()}     Rows: {self.row_count}", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _build_pdf(
    col_names: list[str], col_display: list[str], col_types: dict[str, str], rows: list[dict]
) -> tuple[bytes, str, str]:
    pdf = _ExportPDF(orientation="L", unit="mm", format="A4")
    pdf.row_count = len(rows)
    pdf.set_auto_page_break(auto=True, margin=12)

    usable_w = pdf.w - pdf.l_margin - pdf.r_margin  # landscape A4 ≈ 277 mm
    n = len(col_names)

    if n == 0:
        pdf.add_page()
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, "No data to export.", new_x="LMARGIN", new_y="NEXT")
    else:
        # Proportional column widths; minimum 18 mm per column
        min_col = 18
        max_col = usable_w / n
        col_w = min(max_col, max(min_col, usable_w / max(n, 1)))

        # If columns don't fit on one line, wrap to multi-row header
        header_rows = []
        current_row: list[str] = []
        current_w: list[float] = []
        used = 0.0
        for i in range(n):
            if current_row and used + col_w > usable_w:
                header_rows.append((current_row[:], current_w[:]))
                current_row.clear()
                current_w.clear()
                used = 0.0
            current_row.append(str(col_display[i]))
            current_w.append(col_w)
            used += col_w
        if current_row:
            header_rows.append((current_row, current_w))

        def _draw_row(cells: list[str], widths: list[float], is_header: bool = False) -> None:
            row_h = 5.5 if is_header else 5
            start_x = pdf.get_x()
            start_y = pdf.get_y()
            max_h = row_h

            for text, w in zip(cells, widths):
                pdf.rect(start_x, start_y, w, max_h, style="D" if is_header else "")
                pdf.set_xy(start_x, start_y + 0.4)
                pdf.set_font("Helvetica", "B" if is_header else "", 8)
                pdf.multi_cell(w, 3.2, text, align="L")
                start_x += w
            pdf.set_xy(pdf.l_margin, start_y + max_h)

        pdf.add_page()
        for h_cells, h_widths in header_rows:
            _draw_row(h_cells, h_widths, is_header=True)

        for row in rows:
            cells = []
            widths = []
            for c in col_names:
                val = row.get(c, "")
                if val is None:
                    val = ""
                else:
                    val = str(val)
                cells.append(val)
                widths.append(col_w)
            _draw_row(cells, widths, is_header=False)

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
