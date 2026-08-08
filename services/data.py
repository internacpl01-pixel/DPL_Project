"""Master data service — CRUD for the master table."""
import logging
import time as _time
import asyncpg
from datetime import date as _date
from database import Database

logger = logging.getLogger(__name__)

_MASTER_COLUMNS = [
    "date", "desc", "withdrawal", "deposits", "balance",
    "field_date_1", "field_date_2", "field_date_3",
    "field_date_4", "field_date_5",
    "field_num_1", "field_num_2", "field_num_3", "field_num_4",
    "field_num_5", "field_num_6", "field_num_7", "field_num_8",
    "field_num_9", "field_num_10",
    "field_text_1", "field_text_2", "field_text_3", "field_text_4",
    "field_text_5", "field_text_6", "field_text_7", "field_text_8",
    "field_text_9", "field_text_10", "field_text_11", "field_text_12",
    "field_text_13", "field_text_14", "field_text_15", "field_text_16",
    "field_text_17", "field_text_18", "field_text_19", "field_text_20",
]

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
        "columns": [{"name": c, "type": ""} for c in col_names],
        "page": (offset // limit) + 1,
        "limit": limit,
        "total": total,
    }


async def insert_master_row(conn, row_data: dict) -> int:
    live_cols = await get_live_columns()
    available_cols = {c["name"]: c["type"] for c in live_cols}

    key_to_col = {
        "date": "date",
        "description": "desc",
        "withdrawal": "withdrawal",
        "deposits": "deposits",
        "balance": "balance",
        "reference_no": None,
    }

    columns, values = [], []
    for key, col_name in key_to_col.items():
        if col_name and col_name in available_cols:
            val = row_data.get(key, "")
            if val != "" and val is not None:
                sql_type = available_cols[col_name]
                if sql_type in ("real", "double precision", "numeric"):
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        val = None
                elif sql_type == "integer":
                    try:
                        val = int(float(val))
                    except (TypeError, ValueError):
                        val = None
                elif sql_type == "date":
                    if isinstance(val, str) and val:
                        try:
                            parts = val.split("-")
                            val = _date(int(parts[0]), int(parts[1]), int(parts[2]))
                        except (ValueError, IndexError):
                            val = None
                if val is not None:
                    columns.append(col_name)
                    values.append(val)

    if not columns:
        return -1

    cols_str = ", ".join(f'"{c}"' if c == 'desc' else c for c in columns)
    placeholders = ", ".join([f"${i+1}" for i in range(len(values))])
    new_id = await conn.fetchval(
        f"INSERT INTO master ({cols_str}) VALUES ({placeholders}) RETURNING id",
        *values,
    )
    return new_id


async def insert_master_rows_bulk(conn, rows: list) -> int:
    if not rows:
        return 0

    live_cols = await get_live_columns()
    available_cols = {c["name"]: c["type"] for c in live_cols}

    key_to_col = {
        "date": "date",
        "description": "desc",
        "withdrawal": "withdrawal",
        "deposits": "deposits",
        "balance": "balance",
        "reference_no": None,
    }

    cols_set = set()
    row_templates = []

    for row_data in rows:
        if not isinstance(row_data, dict):
            continue
        columns, values = [], []
        for key, col_name in key_to_col.items():
            if col_name and col_name in available_cols:
                val = row_data.get(key, "")
                if val != "" and val is not None:
                    sql_type = available_cols[col_name]
                    if sql_type in ("real", "double precision", "numeric"):
                        try:
                            val = float(val)
                        except (TypeError, ValueError):
                            val = None
                    elif sql_type == "integer":
                        try:
                            val = int(float(val))
                        except (TypeError, ValueError):
                            val = None
                    elif sql_type == "date":
                        if isinstance(val, str) and val:
                            try:
                                parts = val.split("-")
                                val = _date(int(parts[0]), int(parts[1]), int(parts[2]))
                            except (ValueError, IndexError):
                                val = None
                    if val is not None:
                        columns.append(col_name)
                        values.append(val)
        if not columns:
            continue
        cols_set.update(columns)
        row_templates.append((columns, values))

    if not row_templates:
        return 0

    cols_list = sorted(cols_set)
    col_indices = {c: i for i, c in enumerate(cols_list)}
    cols_str = ", ".join(f'"{c}"' if c == 'desc' else c for c in cols_list)

    all_vals = []
    row_placeholders = []
    param_index = 1
    for columns, values in row_templates:
        row = [None] * len(cols_list)
        for i, col in enumerate(columns):
            row[col_indices[col]] = values[i]
        all_vals.extend(row)
        row_placeholders.append("(" + ", ".join([f"${param_index + i}" for i in range(len(cols_list))]) + ")")
        param_index += len(cols_list)

    placeholders_sql = ", ".join(row_placeholders)
    try:
        await conn.execute(
            f"INSERT INTO master ({cols_str}) VALUES {placeholders_sql}",
            *all_vals,
        )
        return len(row_templates)
    except Exception as e:
        logger.error(f"Bulk insert failed: {e}")
        return 0


async def delete_master_row(row_id: int) -> bool:
    result = await Database.execute("DELETE FROM master WHERE id=$1", row_id)
    return result != "DELETE 0"


async def get_next_field_number(field_type: str, conn) -> int:
    prefix_map = {"date": "field_date", "num": "field_num", "text": "field_text"}
    prefix = prefix_map.get(field_type, "")
    rows = await conn.fetch(
        f"SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = 'master' AND column_name LIKE '{prefix}_%' ORDER BY column_name"
    )
    max_num = 0
    for row in rows:
        try:
            num = int(row["column_name"].split("_")[-1])
            if num > max_num:
                max_num = num
        except ValueError:
            pass
    return max_num + 1


async def add_custom_field(field_type: str) -> str:
    type_map = {"date": ("DATE", "field_date"), "num": ("REAL", "field_num"), "text": ("TEXT", "field_text")}
    sql_type, prefix = type_map.get(field_type, ("TEXT", "field_text"))

    from database import _connect
    conn = await _connect()
    try:
        next_num = await get_next_field_number(field_type, conn)
        col_name = f"{prefix}_{next_num}"
        await conn.execute(
            f"ALTER TABLE master ADD COLUMN IF NOT EXISTS {col_name} {sql_type}"
        )
        _invalidate_live_cols_cache()
        return col_name
    finally:
        await conn.close()
