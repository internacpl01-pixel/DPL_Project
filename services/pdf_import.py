"""PDF import service — parsing and saving to master."""
import asyncio
import logging
import time
from import_helpers import append_rows_to_master, normalize_headers, resolve_field_map
from services.mappings import get_field_mappings
from database import Database

logger = logging.getLogger(__name__)


async def process_pdf_import(file_bytes: bytes, save: bool = False, password: str = ""):
    t_start = time.perf_counter()

    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _parse_sync, file_bytes, password),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        logger.error("[PDF] Parser timed out after 180s")
        raise RuntimeError("PDF parsing timed out (>180s). Try a smaller file or ensure it's a text-based PDF (not scanned).")
    t_parse = (time.perf_counter() - t0) * 1000

    rows = result.get("rows", [])
    bank = result.get("bank", "Unknown")
    logger.info(f"[PDF] parse: {t_parse:.0f}ms, bank={bank}, rows={len(rows)}")

    inserted_count = 0
    if save and rows:
        t1 = time.perf_counter()
        async with Database.acquire() as conn:
            fieldmap_rows = await get_field_mappings()
            t_fm = (time.perf_counter() - t1) * 1000
            t2 = time.perf_counter()
            inserted_count = await append_rows_to_master(conn, rows, fieldmap_rows)
            t_ins = (time.perf_counter() - t2) * 1000
            logger.info(f"[PDF] fieldmap: {t_fm:.0f}ms, insert: {t_ins:.0f}ms, count={inserted_count}")

    t_total = (time.perf_counter() - t_start) * 1000
    logger.info(f"[PDF] TOTAL: {t_total:.0f}ms")
    return {"bank": bank, "rows": rows, "row_count": len(rows), "inserted": inserted_count}


def _parse_sync(file_bytes: bytes, password: str = "") -> dict:
    from parsers import parse_pdf
    return parse_pdf(file_bytes, password=password)