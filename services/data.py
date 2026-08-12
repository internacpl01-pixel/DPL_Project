"""Master data service — CRUD for the master table."""
import logging
import time as _time
from database import Database

logger = logging.getLogger(__name__)


_LIVE_COLS_CACHE = {"cols": None, "expires_at": 0}
_LIVE_COLS_TTL = 30

_TOTAL_COUNT_CACHE = {"total": None, "expires_at": 0}
_TOTAL_COUNT_TTL = 15


def _q(col: str) -> str:
    """Quote a master column for interpolation. `desc` is a reserved word;
    the rest are plain lowercase identifiers."""
    return f'"{col}"' if col == "desc" else col


def _invalidate_live_cols_cache():
    _LIVE_COLS_CACHE["cols"] = None
    _LIVE_COLS_CACHE["expires_at"] = 0


def _invalidate_total_count_cache():
    _TOTAL_COUNT_CACHE["total"] = None
    _TOTAL_COUNT_CACHE["expires_at"] = 0


async def _get_total_count() -> int:
    """Row count for pagination — cached briefly so clicking through pages
    doesn't re-scan the whole table on every click. Invalidated on any
    insert/delete/truncate so it never drifts more than the TTL."""
    now = _time.time()
    cached = _TOTAL_COUNT_CACHE
    if cached["total"] is not None and now < cached["expires_at"]:
        return cached["total"]

    total_row = await Database.fetchrow("SELECT COUNT(*) FROM master")
    total = total_row["count"] if total_row else 0
    cached["total"] = total
    cached["expires_at"] = now + _TOTAL_COUNT_TTL
    return total


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


async def get_master_rows(limit: int = 50, offset: int = 0, search: str = "") -> dict:
    t0 = _time.time()

    live_cols = await get_live_columns()
    col_names = [c["name"] for c in live_cols]
    type_map = {c["name"]: c["type"] for c in live_cols}

    # Build fieldname -> displayname map
    fieldmap_rows = await get_field_mappings()
    display_map = {r["fieldname"]: r.get("displayname", r["fieldname"]) for r in fieldmap_rows}

    cols_str = ", ".join(_q(c) for c in col_names)

    # The search runs here, in SQL — not in the browser. The client only ever
    # holds one page, so filtering there can never see a match on any other
    # page. The count has to come from the same WHERE clause too, otherwise
    # the pagination bar advertises pages that hold no matches.
    search = (search or "").strip()
    params = []
    where_sql = ""
    if search:
        # strpos, not ILIKE: the term is raw user input, and % / _ would be
        # read as wildcards. concat_ws over every live column keeps the old
        # client-side rule (join all values, case-insensitive substring), and
        # its implicit cast means DATE and NUMERIC match the way they display.
        cat = "concat_ws(' ', " + ", ".join(_q(c) for c in col_names) + ")"
        where_sql = f" WHERE strpos(lower({cat}), lower($1)) > 0"
        params.append(search)

    if search:
        # Not cached — the cache holds the unfiltered count, and a per-term
        # cache would only pay off if the same term were searched twice.
        total = await Database.fetchval(f"SELECT COUNT(*) FROM master{where_sql}", *params)
    else:
        total = await _get_total_count()

    rows = await Database.fetch(
        f"SELECT {cols_str} FROM master{where_sql} "
        f"ORDER BY id DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
        *params, limit, offset,
    )
    result_rows = []
    for record in rows:
        result_rows.append({col: str(record[col]) if record[col] is not None else "" for col in col_names})

    ms = (_time.time() - t0) * 1000
    logger.info(
        f"get_master_rows: page={(offset // limit) + 1}, limit={limit}, "
        f"rows={len(result_rows)}, total={total}, search={search!r}, time={ms:.0f}ms"
    )
    return {
        "rows": result_rows,
        "columns": [
            {"name": c, "displayname": display_map.get(c, c), "type": type_map.get(c, "")}
            for c in col_names
        ],
        "page": (offset // limit) + 1,
        "limit": limit,
        "total": total,
    }


async def delete_master_row(row_id: int) -> bool:
    result = await Database.execute("DELETE FROM master WHERE id=$1", row_id)
    deleted = result != "DELETE 0"
    if deleted:
        _invalidate_total_count_cache()
    return deleted


async def truncate_master() -> int:
    result = await Database.execute("TRUNCATE TABLE master RESTART IDENTITY CASCADE")
    count = await Database.fetchval("SELECT COUNT(*) FROM master")
    _invalidate_total_count_cache()
    return count


async def insert_master_rows_bulk(conn, rows: list) -> int:
    """Thin wrapper — delegates to import_helpers (single source of truth)."""
    from import_helpers import append_rows_to_master
    return await append_rows_to_master(conn, rows, await get_field_mappings())


async def get_field_mappings():
    """Lazy import to avoid circular deps."""
    from services.mappings import get_field_mappings as _get
    return await _get()