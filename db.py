import os

import bcrypt
import psycopg2
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")



DB_NAME = os.getenv("DB_NAME", "dpl_database")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "Post123@")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")


def get_connection():

    if DATABASE_URL:

        return psycopg2.connect(DATABASE_URL)

    conn = psycopg2.connect(
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

    return conn



def create_database():

    if DATABASE_URL:

        print("Database already exists on Supabase (managed). Skipping creation.")
        return

    conn = psycopg2.connect(
        database="postgres",
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

    conn.autocommit = True

    cursor = conn.cursor()

    cursor.execute(
        f"""
        SELECT 1 FROM pg_database
        WHERE datname='{DB_NAME}'
        """
    )

    exists = cursor.fetchone()


    if not exists:

        cursor.execute(
            f"""
            CREATE DATABASE {DB_NAME}
            """
        )

        print("Database created successfully")


    cursor.close()
    conn.close()




def authenticate_user(username, password):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, password_hash, user_level
        FROM users
        WHERE username=%s
        """,
        (username,)
    )

    record = cursor.fetchone()

    cursor.close()
    conn.close()

    if not record:

        return None

    user_id, password_hash, user_level = record

    if bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8")
    ):

        return {
            "id": user_id,
            "username": username,
            "level": user_level
        }

    return None
