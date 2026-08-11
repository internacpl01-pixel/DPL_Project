"""Master data service — CRUD for the master table."""
import logging
import time as _time
from database import Database

logger = logging.getLogger(__name__)


_LIVE_COLS_CACHE = {"cols": None, "expires_at": 0}
_LIVE_COLS_TTL = 30


def _invalidate_live_cols_cache():
    _LIVE_COLS_CACHE["cols"] = None
    _LIVE_COLS_CACHE["expires_at"] = 0


async def get_live_columns() -> list:
    now = _time.time()
    cached = _LIVE_COLS_CACHE
    if cached["cols"] is not None and now < cached["expires_at"]:
        return cached["cols"]

    rows = await Database.fetch("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'master'
        ORDER BY ordinal_position
    """)
    cols = [{"name": r["column_name"], "type": r["data_type"], "nullable": r["is_nullable"]} for r in rows]
    cached["cols"] = cols
    cached["expires_at"] = now + _LIVE_COLS_TTL
    return cols


async def get_master_rows(limit: int = 50, offset: int = 0) -> dict:
    t0 = _time.time()
    total_row = await Database.fetchrow("SELECT COUNT(*) FROM master")
    total = total_row["count"] if total_row else 0

    live_cols = await get_live_columns()
    col_names = [c["name"] for c in live_cols]

    # Build fieldname -> displayname map
    fieldmap_rows = await get_field_mappings()
    display_map = {r["fieldname"]: r.get("displayname", r["fieldname"]) for r in fieldmap_rows}

    cols_str = ", ".join(f'"{c}"' if c == "desc" else c for c in col_names)

    rows = await Database.fetch(
        f"SELECT {cols_str} FROM master ORDER BY id DESC LIMIT $1 OFFSET $2",
        limit, offset,
    )
    result_rows = []
    for record in rows:
        result_rows.append({col: str(record[col]) if record[col] is not None else "" for col in col_names})

    ms = (_time.time() - t0) * 1000
    logger.info(
        f"get_master_rows: page={(offset // limit) + 1}, limit={limit}, "
        f"rows={len(result_rows)}, total={total}, time={ms:.0f}ms"
    )
    return {
        "rows": result_rows,
        "columns": [
            {"name": c, "displayname": display_map.get(c, c), "type": ""}
            for c in col_names
        ],
        "page": (offset // limit) + 1,
        "limit": limit,
        "total": total,
    }


async def delete_master_row(row_id: int) -> bool:
    result = await Database.execute("DELETE FROM master WHERE id=$1", row_id)
    return result != "DELETE 0"


async def truncate_master() -> int:
    result = await Database.execute("TRUNCATE TABLE master RESTART IDENTITY CASCADE")
    count = await Database.fetchval("SELECT COUNT(*) FROM master")
    return count


async def insert_master_rows_bulk(conn, rows: list) -> int:
    """Thin wrapper — delegates to import_helpers (single source of truth)."""
    from import_helpers import append_rows_to_master
    return await append_rows_to_master(conn, rows, await get_field_mappings())


async def get_field_mappings():
    """Lazy import to avoid circular deps."""
    from services.mappings import get_field_mappings as _get
    return await _get()