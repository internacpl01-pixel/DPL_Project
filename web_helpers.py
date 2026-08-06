from db import get_connection
from models import get_user_level_name, log_field_change



def get_field_mappings():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, fieldname, displayname, mapfields
        FROM fieldmap
        ORDER BY id
        """
    )

    records = cursor.fetchall()

    cursor.close()
    conn.close()

    return [
        {
            "id": r[0],
            "fieldname": r[1],
            "displayname": r[2],
            "mapfields": r[3],
        }

        for r in records
    ]



def get_table_structure():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'master'
        ORDER BY ordinal_position
        """
    )

    records = cursor.fetchall()

    cursor.close()
    conn.close()

    return [
        {
            "column_name": r[0],
            "data_type": r[1],
            "is_nullable": r[2],
        }

        for r in records
    ]



def get_change_log():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, fieldname, table_row_id, table_name, changed_at
        FROM fieldchange_log
        ORDER BY id DESC
        """
    )

    records = cursor.fetchall()

    cursor.close()
    conn.close()

    return [
        {
            "id": r[0],
            "fieldname": r[1],
            "table_row_id": r[2],
            "table_name": r[3],
            "changed_at": str(r[4]),
        }

        for r in records
    ]



def get_all_users_data():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, username, user_level, created_at
        FROM users
        ORDER BY id
        """
    )

    records = cursor.fetchall()

    cursor.close()
    conn.close()

    return [
        {
            "id": r[0],
            "username": r[1],
            "user_level": r[2],
            "level_name": get_user_level_name(r[2]),
            "created_at": str(r[3]),
        }

        for r in records
    ]



def update_field_mapping(fieldname, displayname, mapfields):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE fieldmap
        SET displayname=%s, mapfields=%s
        WHERE fieldname=%s
        """,
        (displayname, mapfields, fieldname),
    )

    conn.commit()

    cursor.execute(
        """
        SELECT id
        FROM fieldmap
        WHERE fieldname=%s
        """,
        (fieldname,),
    )

    record = cursor.fetchone()

    cursor.close()
    conn.close()

    if record:

        log_field_change(fieldname, record[0], "fieldmap")



def change_user_level_by_id(user_id, new_level):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users
        SET user_level=%s
        WHERE id=%s
        """,
        (new_level, user_id),
    )

    conn.commit()

    cursor.close()
    conn.close()
