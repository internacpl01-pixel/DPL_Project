"""Data router — master table CRUD."""
from fastapi import APIRouter, Depends, HTTPException, status

from schemas import AddDataRequest
from dependencies import get_current_user
from database import Database
from services.data import get_master_rows, insert_master_rows_bulk, delete_master_row

router = APIRouter(prefix="/api", tags=["data"])


@router.get("/data")
async def list_data(page: int = 1, limit: int = 50, current_user: dict = Depends(get_current_user)):
    page = max(1, page)
    limit = max(1, min(500, limit))
    offset = (page - 1) * limit
    return await get_master_rows(limit=limit, offset=offset)


@router.post("/data")
async def add_data(body: AddDataRequest, current_user: dict = Depends(get_current_user)):
    rows = body.rows or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rows array is required and cannot be empty")

    async with Database.acquire() as conn:
        count = await insert_master_rows_bulk(conn, rows)
    return {"inserted": count}


@router.delete("/data/{row_id}")
async def delete_data(row_id: int, current_user: dict = Depends(get_current_user)):
    deleted = await delete_master_row(row_id)
    if deleted:
        return {"message": f"Row {row_id} deleted"}
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Row {row_id} not found")
