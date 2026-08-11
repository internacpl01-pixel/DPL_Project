"""
Import helpers for bank statement PDF processing.
Handles field resolution via the existing fieldmap table,
and appending rows to the master table using type-aware normalization.
"""

import logging
import re
from datetime import date as _date
from decimal import Decimal, InvalidOperation

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


async def append_rows_to_master(conn, rows: list, fieldmap_rows: list, live_cols: dict = None) -> int:
    """
    Insert parsed rows into master table.
    Row keys are already fieldmap fieldnames (master column names).
    Type-aware coercion: dates → DATE, numerics → REAL, rest → TEXT.
    Chunked INSERTs stay under asyncpg's 32,767 parameter limit.

    live_cols: optional {column_name: data_type} map. Callers that already
    fetched this (pdf_import.py, excel_import.py) can pass it through to
    skip a duplicate information_schema query. Falls back to querying it
    here when not provided, so existing callers are unaffected.
    """
    if not rows:
        return 0

    if live_cols is None:
        live_col_rows = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'master' ORDER BY ordinal_position"
        )
        live_cols = {r["column_name"]: r["data_type"] for r in live_col_rows}

    # Collect all column keys from rows, filter to live columns only
    all_keys = set()
    for row in rows:
        all_keys.update(k for k in row.keys() if k in live_cols and k != "_date_raw")

    cols_list = sorted(all_keys)
    if not cols_list:
        return 0

    col_indices = {c: i for i, c in enumerate(cols_list)}

    # Build flat values array, coercing types per column
    flat_values = []
    dropped_count = 0
    for row in rows:
        row_vals = [None] * len(cols_list)
        for col in cols_list:
            val = row.get(col)
            if val is None or val == "":
                continue

            col_type = live_cols.get(col, "text").lower()

            if col_type in ("date", "timestamp without time zone", "timestamp"):
                d = _parse_date_to_date(val)
                if d is None:
                    continue
                row_vals[col_indices[col]] = d
            elif col_type in ("real", "double precision", "numeric", "integer", "bigint"):
                try:
                    cleaned = str(val).replace(",", "").strip()
                    if cleaned:
                        row_vals[col_indices[col]] = Decimal(cleaned).quantize(Decimal("0.01"))
                except (ValueError, TypeError, InvalidOperation):
                    continue
            else:
                # TEXT or other — keep as string
                row_vals[col_indices[col]] = str(val).strip()

        if any(v is not None for v in row_vals):
            flat_values.extend(row_vals)
        else:
            dropped_count += 1

    if not flat_values:
        return 0

    # Chunked INSERT
    CHUNK_SIZE = 200
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

    if dropped_count:
        logger.warning(f"[import_helpers] Dropped {dropped_count} rows where all values failed type coercion")

    if total_inserted:
        from services.data import _invalidate_total_count_cache
        _invalidate_total_count_cache()

    return total_inserted


def compute_fill_rates(rows: list) -> dict:
    """Per-field fill rate: how many of the assembled rows have a value for
    each key. Shared by the PDF and Excel import paths (identical logic)."""
    total_rows = len(rows)
    if not total_rows:
        return {}
    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    return {
        key: {"filled": sum(1 for r in rows if r.get(key)), "total": total_rows}
        for key in sorted(all_keys)
    }
