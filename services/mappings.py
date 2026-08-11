"""Field mapping and table-structure service."""
import time as _time
from database import Database

_FIELD_MAPPINGS_CACHE = {"rows": None, "expires_at": 0}
_FIELD_MAPPINGS_TTL = 60


def _invalidate_field_mappings_cache():
    _FIELD_MAPPINGS_CACHE["rows"] = None
    _FIELD_MAPPINGS_CACHE["expires_at"] = 0


async def get_field_mappings() -> list:
    now = _time.time()
    cached = _FIELD_MAPPINGS_CACHE
    if cached["rows"] is not None and now < cached["expires_at"]:
        return cached["rows"]

    rows = await Database.fetch(
        "SELECT id, fieldname, displayname, mapfields FROM fieldmap ORDER BY id"
    )
    result = [
        {"id": r["id"], "fieldname": r["fieldname"], "displayname": r["displayname"], "mapfields": r["mapfields"]}
        for r in rows
    ]
    cached["rows"] = result
    cached["expires_at"] = now + _FIELD_MAPPINGS_TTL
    return result


async def get_table_structure() -> list:
    """Same underlying query as services.data.get_live_columns — routed
    through its cache instead of re-querying information_schema directly.
    Response shape is unchanged (column_name/data_type/is_nullable keys)."""
    from services.data import get_live_columns
    live_cols = await get_live_columns()
    return [
        {"column_name": c["name"], "data_type": c["type"], "is_nullable": c["nullable"]}
        for c in live_cols
    ]


async def get_change_log() -> list:
    rows = await Database.fetch("""
        SELECT id, fieldname, table_row_id, table_name, changed_at
        FROM fieldchange_log
        ORDER BY id DESC
    """)
    return [
        {"id": r["id"], "fieldname": r["fieldname"], "table_row_id": r["table_row_id"],
         "table_name": r["table_name"], "changed_at": str(r["changed_at"])}
        for r in rows
    ]


async def log_field_change(fieldname: str, table_row_id: int, table_name: str):
    await Database.execute(
        "INSERT INTO fieldchange_log (fieldname, table_row_id, table_name) VALUES ($1, $2, $3)",
        fieldname, table_row_id, table_name,
    )


async def update_field_mapping(fieldname: str, displayname: str, mapfields: str):
    record = await Database.fetchrow(
        "UPDATE fieldmap SET displayname=$1, mapfields=$2 WHERE fieldname=$3 RETURNING id",
        displayname, mapfields, fieldname,
    )
    _invalidate_field_mappings_cache()
    if record:
        await log_field_change(fieldname, record["id"], "fieldmap")
