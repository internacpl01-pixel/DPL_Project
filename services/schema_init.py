"""Database schema initialization (DDL)."""
import bcrypt
from database import Database


async def create_tables():
    async with Database.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fieldmap (
                id SERIAL PRIMARY KEY,
                fieldname VARCHAR(100) UNIQUE NOT NULL,
                displayname VARCHAR(100) NOT NULL,
                mapfields TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fieldchange_log (
                id SERIAL PRIMARY KEY,
                fieldname VARCHAR(100) NOT NULL,
                table_row_id INTEGER NOT NULL,
                table_name VARCHAR(100) NOT NULL,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS master (
                id SERIAL PRIMARY KEY
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                user_level INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # No statement columns are created here. Every field on master is a
        # custom field the user adds through the Custom Fields page, so a fresh
        # database starts as a bare id and an empty fieldmap. Existing
        # databases are untouched — this only ever added missing columns.
        has_id = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name=$1 AND column_name=$2",
            "master", "id",
        )
        if not has_id:
            await conn.execute("ALTER TABLE master ADD COLUMN id SERIAL PRIMARY KEY")
        if not await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE tablename=$1 AND indexname=$2",
            "master", "idx_master_id",
        ):
            await conn.execute("CREATE INDEX idx_master_id ON master(id)")
        if not await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE tablename=$1 AND indexname=$2",
            "fieldchange_log", "idx_fieldchange_log_changed_at",
        ):
            await conn.execute(
                "CREATE INDEX idx_fieldchange_log_changed_at ON fieldchange_log(changed_at DESC)"
            )


async def create_default_admin():
    user = await Database.fetchrow("SELECT id FROM users WHERE username=$1", "admin")
    if not user:
        password_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode("utf-8")
        await Database.execute(
            "INSERT INTO users (username, password_hash, user_level) VALUES ($1, $2, $3)",
            "admin", password_hash, 2,
        )
