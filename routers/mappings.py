"""Field mappings router — list, update, delete mapfield, custom fields, table structure, change log."""
from fastapi import APIRouter, Depends, HTTPException, Request, status

from dependencies import get_current_user, require_level
from schemas import UpdateMappingRequest, CustomFieldRequest
from services.mappings import (
    get_field_mappings,
    get_table_structure,
    get_change_log,
    update_field_mapping,
    _invalidate_field_mappings_cache,
)
from database import Database

router = APIRouter(prefix="/api", tags=["mappings"])


@router.get("/field-mappings")
async def list_field_mappings(current_user: dict = Depends(get_current_user)):
    return await get_field_mappings()


@router.put("/field-mappings/{fieldname}")
async def update_mapping_endpoint(
    fieldname: str,
    body: UpdateMappingRequest,
    current_user: dict = Depends(require_level(1, 2)),
):
    displayname = body.displayname.strip()
    mapfields = body.mapfields.strip()

    async with Database.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT id, displayname, mapfields FROM fieldmap WHERE fieldname=$1",
            fieldname,
        )

        if not record:
            new_displayname = displayname.strip() if displayname.strip() else fieldname
            await conn.execute(
                "INSERT INTO fieldmap (fieldname, displayname, mapfields) VALUES ($1, $2, $3)",
                fieldname, new_displayname, "",
            )
            record = {"id": None, "displayname": new_displayname, "mapfields": ""}

        existing_displayname = record["displayname"]
        existing_mapfields = record["mapfields"]

        if not displayname:
            displayname = existing_displayname

        if mapfields:
            new_items = [item.strip() for item in mapfields.split(",") if item.strip()]
            existing_items = [item.strip() for item in existing_mapfields.split(",") if item.strip()]
            duplicates_in_row = [item for item in new_items if item in existing_items]
            if duplicates_in_row:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Duplicate values in same row: {', '.join(duplicates_in_row)}",
                )
            other_rows = await conn.fetch(
                "SELECT mapfields FROM fieldmap WHERE fieldname != $1 AND mapfields != ''",
                fieldname,
            )
            existing_values = set()
            for row in other_rows:
                for v in row["mapfields"].split(","):
                    v = v.strip()
                    if v:
                        existing_values.add(v.lower())
            duplicates_across = [item for item in new_items if item.lower() in existing_values]
            if duplicates_across:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Values already exist in another mapping: {', '.join(duplicates_across)}",
                )
            mapfields = existing_mapfields + ", " + ", ".join(new_items) if existing_mapfields else ", ".join(new_items)
        else:
            mapfields = existing_mapfields

    await update_field_mapping(fieldname, displayname, mapfields)
    return {"message": "Mapping updated successfully"}


@router.delete("/field-mappings/{fieldname}/mapfield")
async def delete_mapfield(
    fieldname: str,
    request: Request,
    current_user: dict = Depends(require_level(1, 2)),
):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    value_to_remove = (body.get("value") or "").strip()
    if not value_to_remove:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Value to remove is required")

    async with Database.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT id, displayname, mapfields FROM fieldmap WHERE fieldname=$1",
            fieldname,
        )
        if not record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field not found")

        existing_displayname = record["displayname"]
        existing_mapfields = record["mapfields"]
        items = [item.strip() for item in existing_mapfields.split(",") if item.strip()]

        if value_to_remove not in items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Value '{value_to_remove}' not found in mapfields",
            )

        items = [item for item in items if item != value_to_remove]
        new_mapfields = ", ".join(items)

    await update_field_mapping(fieldname, existing_displayname, new_mapfields)
    return {"message": f"Removed '{value_to_remove}' from mapfields"}


@router.get("/table-structure")
async def table_structure(current_user: dict = Depends(get_current_user)):
    return await get_table_structure()


@router.get("/change-log")
async def change_log(current_user: dict = Depends(require_level(1, 2))):
    return await get_change_log()


@router.post("/custom-fields")
async def create_custom_field(body: CustomFieldRequest, current_user: dict = Depends(get_current_user)):
    field_type = body.type.strip().lower()
    if field_type not in ("date", "num", "text"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid type. Use date, num, or text")

    type_map = {"date": ("DATE", "field_date"), "num": ("REAL", "field_num"), "text": ("TEXT", "field_text")}
    sql_type, prefix = type_map[field_type]

    async with Database.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'master' AND column_name LIKE $1 ORDER BY column_name",
            prefix + "_%",
        )
        max_num = 0
        for row in rows:
            try:
                num = int(row["column_name"].split("_")[-1])
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
        next_num = max_num + 1
        col_name = f"{prefix}_{next_num}"
        await conn.execute(
            "ALTER TABLE master ADD COLUMN IF NOT EXISTS " + col_name + " " + sql_type
        )
        from services.data import _invalidate_live_cols_cache
        _invalidate_live_cols_cache()
        # Also invalidate fieldmap cache (the new fieldmap row won't be visible until TTL expires)
        _invalidate_field_mappings_cache()
        await conn.execute(
            "INSERT INTO fieldmap (fieldname, displayname, mapfields) VALUES ($1, $2, $3) "
            "ON CONFLICT (fieldname) DO UPDATE SET displayname=EXCLUDED.displayname, mapfields=EXCLUDED.mapfields",
            col_name, col_name, col_name,
        )

    return {"column": col_name, "type": sql_type}
