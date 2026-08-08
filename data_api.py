"""
Master data CRUD operations.
Handles reading and writing rows in the master table.
No duplicate checking — pure append for inserts.
"""

import logging
from db import get_connection

logger = logging.getLogger(__name__)

# All known master column names (fixed + custom)
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


def get_master_columns() -> list:
    """Return the list of known master column names."""
    return list(_MASTER_COLUMNS)


def get_live_columns(conn) -> list:
    """Query information_schema for columns that actually exist in master right now."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'master'
        ORDER BY ordinal_position
    """)
    cols = [{"name": r[0], "type": r[1], "nullable": r[2]} for r in cursor.fetchall()]
    cursor.close()
    return cols


def get_master_rows(limit: int = 50, offset: int = 0) -> dict:
    """Get paginated master rows with total count."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM master")
    total = cursor.fetchone()[0]

    live_cols = get_live_columns(conn)

    col_names = [c["name"] for c in live_cols]
    cols_str = ", ".join(f'"{c}"' if c == "desc" else c for c in col_names)

    cursor.execute(
        f'SELECT {cols_str} FROM master ORDER BY id DESC LIMIT %s OFFSET %s',
        (limit, offset),
    )

    rows = []
    for record in cursor.fetchall():
        row = {}
        for i, col in enumerate(col_names):
            val = record[i]
            row[col] = str(val) if val is not None else ""
        rows.append(row)

    cursor.close()
    conn.close()

    return {
        "rows": rows,
        "columns": [{"name": c, "type": ""} for c in col_names],
        "page": (offset // limit) + 1,
        "limit": limit,
        "total": total,
    }


def insert_master_row(conn, row_data: dict) -> int:
    """
    Insert a single row into master.
    Only inserts columns that have values (avoids NULL-only rows).
    Returns the new row id.
    """
    live_cols = get_live_columns(conn)
    available_cols = {c["name"] for c in live_cols}

    columns = []
    values = []

    # Map normalized keys to master columns
    key_to_col = {
        "date": "date",
        "description": "desc",
        "withdrawal": "withdrawal",
        "deposits": "deposits",
        "balance": "balance",
        "reference_no": None,  # not a master column — skip
    }

    for key, col_name in key_to_col.items():
        if col_name and col_name in available_cols:
            val = row_data.get(key, "")
            if val:
                columns.append(col_name)
                values.append(val)

    if not columns:
        return -1

    cols_str = ", ".join(
        f'"{c}"' if c == 'desc' else c for c in columns
    )
    placeholders = ", ".join(["%s"] * len(values))

    cursor = conn.cursor()
    cursor.execute(
        f"INSERT INTO master ({cols_str}) VALUES ({placeholders}) RETURNING id",
        tuple(values),
    )
    new_id = cursor.fetchone()[0]
    cursor.close()

    return new_id


def insert_master_rows_bulk(conn, rows: list) -> int:
    """
    Insert multiple rows into master.
    Returns the number of rows inserted.
    """
    if not rows:
        return 0

    live_cols = get_live_columns(conn)
    available_cols = {c["name"] for c in live_cols}

    key_to_col = {
        "date": "date",
        "description": "desc",
        "withdrawal": "withdrawal",
        "deposits": "deposits",
        "balance": "balance",
        "reference_no": None,
    }

    cursor = conn.cursor()
    inserted = 0

    for row_data in rows:
        columns = []
        values = []

        for key, col_name in key_to_col.items():
            if col_name and col_name in available_cols:
                val = row_data.get(key, "")
                if val:
                    columns.append(col_name)
                    values.append(val)

        if not columns:
            continue

        cols_str = ", ".join(
            f'"{c}"' if c == 'desc' else c for c in columns
        )
        placeholders = ", ".join(["%s"] * len(values))

        try:
            cursor.execute(
                f"INSERT INTO master ({cols_str}) VALUES ({placeholders})",
                tuple(values),
            )
            inserted += 1
        except Exception as e:
            logger.warning(f"Bulk insert row failed: {e}")

    cursor.close()
    return inserted


def delete_master_row(row_id: int) -> bool:
    """Delete a single master row by id. Returns True if deleted."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM master WHERE id=%s", (row_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    cursor.close()
    conn.close()
    return deleted
