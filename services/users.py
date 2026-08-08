"""User management service."""
import bcrypt
from database import Database
from services.auth import get_user_level_name


async def list_users() -> list:
    rows = await Database.fetch(
        "SELECT id, username, user_level, created_at FROM users ORDER BY id"
    )
    return [
        {
            "id": r["id"],
            "username": r["username"],
            "user_level": r["user_level"],
            "level_name": await get_user_level_name(r["user_level"]),
            "created_at": str(r["created_at"]),
        }
        for r in rows
    ]


async def update_user_level(user_id: int, new_level: int) -> bool:
    record = await Database.fetchrow(
        "SELECT username FROM users WHERE id=$1", user_id
    )
    if not record:
        return False
    await Database.execute(
        "UPDATE users SET user_level=$1 WHERE id=$2", new_level, user_id
    )
    return True


async def edit_user(user_id: int, username: str | None = None, password: str | None = None) -> bool:
    sets = []
    params = []

    if username is not None:
        sets.append(f"username=${len(params) + 1}")
        params.append(username)
    if password is not None:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        sets.append(f"password_hash=${len(params) + 1}")
        params.append(password_hash)

    if not sets:
        return False

    params.append(user_id)
    query = f"UPDATE users SET {', '.join(sets)} WHERE id=${len(params)}"
    await Database.execute(query, *params)
    return True


async def delete_user(user_id: int) -> str | None:
    record = await Database.fetchrow(
        "SELECT username FROM users WHERE id=$1", user_id
    )
    if not record:
        return None
    await Database.execute("DELETE FROM users WHERE id=$1", user_id)
    return record["username"]


async def get_user_level(user_id: int) -> int | None:
    row = await Database.fetchrow(
        "SELECT user_level FROM users WHERE id=$1", user_id
    )
    return row["user_level"] if row else None
