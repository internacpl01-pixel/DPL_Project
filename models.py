from db import get_connection
import bcrypt



def create_tables():

    conn = get_connection()
    cursor = conn.cursor()


    # Field Mapping Table

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS fieldmap
        (
            id SERIAL PRIMARY KEY,
            fieldname VARCHAR(100) UNIQUE NOT NULL,
            displayname VARCHAR(100) NOT NULL,
            mapfields TEXT NOT NULL
        )
        """
    )


    # Field Change Log Table

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS fieldchange_log
        (
            id SERIAL PRIMARY KEY,
            fieldname VARCHAR(100) NOT NULL,
            table_row_id INTEGER NOT NULL,
            table_name VARCHAR(100) NOT NULL,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


    # Master Table — only id, custom fields added dynamically

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS master
        (
            id SERIAL PRIMARY KEY
        )
        """
    )


    conn.commit()

    cursor.close()
    conn.close()

    print("Tables created successfully")





def insert_default_mappings():

    # No defaults — all fields are added dynamically via Add Custom Field
    print("Default field mappings skipped (custom fields only)")




def create_users_table():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users
        (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            user_level INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    cursor.close()
    conn.close()

    print("Users table created successfully")




def create_default_admin():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM users
        WHERE username=%s
        """,
        ("admin",)
    )

    exists = cursor.fetchone()

    if not exists:

        password_hash = bcrypt.hashpw(
            b"admin123",
            bcrypt.gensalt()
        ).decode("utf-8")

        cursor.execute(
            """
            INSERT INTO users
            (username, password_hash, user_level)
            VALUES (%s, %s, %s)
            """,
            ("admin", password_hash, 2)
        )

        conn.commit()

        print("Default admin user created (username: admin, password: admin123)")

    cursor.close()
    conn.close()




def get_user_level_name(level):

    level_names = {

        0: "Staff",
        1: "Manager",
        2: "Admin"
    }

    return level_names.get(level, "Unknown")


def log_field_change(fieldname, table_row_id, table_name):

    conn = get_connection()
    cursor = conn.cursor()


    cursor.execute(
        """
        INSERT INTO fieldchange_log
        (fieldname, table_row_id, table_name)
        VALUES (%s, %s, %s)
        """,
        (fieldname, table_row_id, table_name)
    )


    conn.commit()
    cursor.close()
    conn.close()



def get_next_field_number(conn, field_type):

    cursor = conn.cursor()

    type_map = {

        "date": "field_date",
        "num": "field_num",
        "text": "field_text"
    }

    prefix = type_map[field_type]

    cursor.execute(
        f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'master'
        AND column_name LIKE '{prefix}_%'
        ORDER BY column_name
        """
    )


    existing = cursor.fetchall()
    max_num = 0

    for row in existing:

        try:

            num = int(row[0].split("_")[-1])

            if num > max_num:

                max_num = num

        except ValueError:

            pass

    cursor.close()

    return max_num + 1


def add_custom_field(field_type):

    conn = get_connection()
    cursor = conn.cursor()

    type_map = {

        "date": ("DATE", "field_date"),
        "num": ("REAL", "field_num"),
        "text": ("TEXT", "field_text")
    }

    sql_type, prefix = type_map[field_type]

    next_num = get_next_field_number(conn, field_type)

    col_name = f"{prefix}_{next_num}"

    cursor.execute(
        f"""
        ALTER TABLE master
        ADD COLUMN IF NOT EXISTS {col_name} {sql_type}
        """
    )


    conn.commit()
    cursor.close()
    conn.close()

    print(f"\nColumn '{col_name}' ({sql_type}) added successfully.")
    return col_name



def get_all_users():

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

    print("\n")
    print("-" * 70)
    print(
        f"{'ID':<5}{'Username':<20}{'Level':<15}{'Created At'}"
    )
    print("-" * 70)

    for row in records:

        level_name = get_user_level_name(row[2])

        print(
            f"{row[0]:<5}{row[1]:<20}{level_name:<15}{row[3]}"
        )

    print("-" * 70)

    cursor.close()
    conn.close()



def change_user_level(username, new_level):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users
        SET user_level=%s
        WHERE username=%s
        """,
        (new_level, username)
    )

    conn.commit()

    cursor.close()
    conn.close()

    print(
        f"\nUser '{username}' level changed to {get_user_level_name(new_level)}"
    )


def update_user_by_id(user_id, username=None, password=None, new_level=None):

    conn = get_connection()
    cursor = conn.cursor()

    sets = []
    params = []

    if username is not None:
        sets.append("username=%s")
        params.append(username)

    if password is not None:
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8")
        sets.append("password_hash=%s")
        params.append(password_hash)

    if new_level is not None:
        sets.append("user_level=%s")
        params.append(new_level)

    if not sets:
        cursor.close()
        conn.close()
        return False

    params.append(user_id)

    cursor.execute(
        f"""
        UPDATE users
        SET {', '.join(sets)}
        WHERE id=%s
        """,
        tuple(params),
    )

    conn.commit()

    cursor.close()
    conn.close()

    return True


def delete_user_by_id(user_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM users WHERE id=%s",
        (user_id,),
    )

    conn.commit()

    deleted = cursor.rowcount > 0

    cursor.close()
    conn.close()

    return deleted

def register_user(username, password, user_level=0):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM users
        WHERE username=%s
        """,
        (username,)
    )

    exists = cursor.fetchone()

    if exists:

        cursor.close()
        conn.close()
        return False

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    cursor.execute(
        """
        INSERT INTO users
        (username, password_hash, user_level)
        VALUES (%s, %s, %s)
        """,
        (username, password_hash, user_level)
    )

    conn.commit()
    cursor.close()
    conn.close()

    return True
