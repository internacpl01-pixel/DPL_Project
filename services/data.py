"""Master data service — CRUD for the master table."""
import logging
import re
import time as _time
from datetime import date as _date
from database import Database
from import_helpers import resolve_field_map, resolve_column
from services.mappings import get_field_mappings

logger = logging.getLogger(__name__)


def _parse_date(val) -> _date | None:
    """Parse various date string formats into a datetime.date object."""
    s = str(val).strip()
    if not s:
        return None
    # YYYY-MM-DD
    if len(s) == 10 and s[4] == "-":
        try:
            return _date.fromisoformat(s)
        except ValueError:
            pass
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{2})[/\-](\d{2})[/\-](\d{4})", s)
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    # DD-Mon-YYYY
    m = re.match(r"(\d{2})-([A-Za-z]{3})-(\d{4})", s)
    if m:
        month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                     "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        mon = month_map.get(m.group(2).lower())
        if mon:
            try:
                return _date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                pass
    return None


_PARSER_KEYS = ("date", "description", "withdrawal", "deposits", "balance")

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

    alias_map = resolve_field_map(await get_field_mappings())

    key_to_col = {}
    for pk in _PARSER_KEYS:
        resolved = resolve_column(pk, alias_map)
        key_to_col[pk] = resolved

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
                        val = _parse_date(val)
                    if val is None:
                        continue
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

    alias_map = resolve_field_map(await get_field_mappings())

    key_to_col = {}
    for pk in _PARSER_KEYS:
        resolved = resolve_column(pk, alias_map)
        key_to_col[pk] = resolved

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
                            val = _parse_date(val)
                        if val is None:
                            continue
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


async def truncate_master() -> int:
    result = await Database.execute("TRUNCATE TABLE master RESTART IDENTITY CASCADE")
    count = await Database.fetchval("SELECT COUNT(*) FROM master")
    return count


async def get_next_field_number(field_type: str, conn) -> int:
    prefix_map = {"date": "field_date", "num": "field_num", "text": "field_text"}
    prefix = prefix_map.get(field_type, "")
    if not prefix:
        return 1
    pattern = prefix + "_%"
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'master' AND column_name LIKE $1 ORDER BY column_name",
        pattern,
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
