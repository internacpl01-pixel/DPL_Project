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
    rows = await Database.fetch("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'master'
        ORDER BY ordinal_position
    """)
    return [
        {"column_name": r["column_name"], "data_type": r["data_type"], "is_nullable": r["is_nullable"]}
        for r in rows
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
    await Database.execute(
        "UPDATE fieldmap SET displayname=$1, mapfields=$2 WHERE fieldname=$3",
        displayname, mapfields, fieldname,
    )
    _invalidate_field_mappings_cache()
    record = await Database.fetchrow("SELECT id FROM fieldmap WHERE fieldname=$1", fieldname)
    if record:
        await log_field_change(fieldname, record["id"], "fieldmap")
