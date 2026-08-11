"""PDF import service — parsing and saving to master."""
import asyncio
import logging
import time
from services.mappings import get_field_mappings
from database import Database

logger = logging.getLogger(__name__)


async def process_pdf_import(file_bytes: bytes, save: bool = False, password: str = ""):
    t_start = time.perf_counter()

    # Fetch fieldmap + live column types BEFORE dispatching to executor
    # (DB access stays in async context; both are needed for header detection)
    fieldmap_rows = await get_field_mappings()
    logger.info(f"[PDF] fieldmap fetch: {len(fieldmap_rows)} mappings")

    live_col_types = {}
    async with Database.acquire() as conn:
        col_rows = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'master' ORDER BY ordinal_position"
        )
        live_col_types = {r["column_name"]: r["data_type"] for r in col_rows}
    logger.info(f"[PDF] live columns: {len(live_col_types)}")

    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    try:
        from parsers import _parse_sync
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _parse_sync, file_bytes, password,
                                 fieldmap_rows, live_col_types),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        logger.error("[PDF] Parser timed out after 180s")
        raise RuntimeError("PDF parsing timed out (>180s). Try a smaller file or ensure it's a text-based PDF (not scanned).")
    t_parse = (time.perf_counter() - t0) * 1000

    rows = result.get("rows", [])
    headers_detected = result.get("headers_detected", {})
    unmapped_headers = result.get("unmapped_headers", [])
    stats = result.get("stats", {})
    logger.info(f"[PDF] parse: {t_parse:.0f}ms, rows={len(rows)}, stats={stats}")

    inserted_count = 0
    if save and rows:
        t1 = time.perf_counter()
        async with Database.acquire() as conn:
            from import_helpers import append_rows_to_master
            inserted_count = await append_rows_to_master(conn, rows, fieldmap_rows)
            t_ins = (time.perf_counter() - t1) * 1000
            logger.info(f"[PDF] insert: {t_ins:.0f}ms, count={inserted_count}")

    # Per-field fill rates: how many rows have a value for each known column
    total_rows = len(rows)
    fill_rates = {}
    if total_rows:
        all_keys = set()
        for r in rows:
            all_keys.update(r.keys())
        for key in sorted(all_keys):
            filled = sum(1 for r in rows if r.get(key))
            fill_rates[key] = {"filled": filled, "total": total_rows}

    t_total = (time.perf_counter() - t_start) * 1000
    logger.info(f"[PDF] TOTAL: {t_total:.0f}ms")
    return {
        "rows": rows,
        "row_count": len(rows),
        "inserted": inserted_count,
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "stats": stats,
        "fill_rates": fill_rates,
    }
