"""PDF import service — parsing and saving to master."""
from import_helpers import append_rows_to_master, normalize_headers, resolve_field_map
from services.mappings import get_field_mappings
from database import Database


async def process_pdf_import(file_bytes: bytes, save: bool = False):
    from parsers import parse_pdf
    result = parse_pdf(file_bytes)
    rows = result.get("rows", [])
    bank = result.get("bank", "Unknown")

    inserted_count = 0
    if save and rows:
        conn = await Database.acquire()
        try:
            fieldmap_rows = await get_field_mappings()
            inserted_count = await append_rows_to_master(conn, rows, fieldmap_rows)
        finally:
            await conn.close()

    return {"bank": bank, "rows": rows, "row_count": len(rows), "inserted": inserted_count}
