import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_NAME = os.getenv("DB_NAME", "dpl_database")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "Post123@")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")


async def _get_dsn():
    if DATABASE_URL:
        return DATABASE_URL
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class Database:
    pool: asyncpg.Pool | None = None

    @classmethod
    async def connect(cls):
        dsn = await _get_dsn()
        cls.pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

    @classmethod
    async def disconnect(cls):
        if cls.pool:
            await cls.pool.close()
            cls.pool = None

    @classmethod
    async def fetch(cls, query: str, *args):
        async with cls.acquire() as conn:
            return await conn.fetch(query, *args)

    @classmethod
    async def fetchrow(cls, query: str, *args):
        async with cls.acquire() as conn:
            return await conn.fetchrow(query, *args)

    @classmethod
    async def fetchval(cls, query: str, *args):
        async with cls.acquire() as conn:
            return await conn.fetchval(query, *args)

    @classmethod
    async def execute(cls, query: str, *args):
        async with cls.acquire() as conn:
            return await conn.execute(query, *args)

    @classmethod
    async def executemany(cls, query: str, args_list: list):
        async with cls.acquire() as conn:
            await conn.executemany(query, args_list)

    @classmethod
    def acquire(cls):
        if cls.pool is None:
            raise RuntimeError("Database not connected.")
        return cls.pool.acquire()

    @classmethod
    async def _ddl(cls, query: str):
        conn = await asyncpg.connect(await _get_dsn())
        try:
            await conn.execute(query)
        finally:
            await conn.close()


async def _connect():
    return await asyncpg.connect(await _get_dsn())


async def get_conn():
    """Return a raw asyncpg connection for DDL / ALTER TABLE statements."""
    return await _connect()
