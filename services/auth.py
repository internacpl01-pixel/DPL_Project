"""Auth service — login, token creation."""
import bcrypt
import os
from datetime import datetime, timedelta, timezone
from jose import jwt

from database import Database

SECRET_KEY = os.environ.get("SECRET_KEY", "dpl-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def authenticate_user(username: str, password: str) -> dict | None:
    row = await Database.fetchrow(
        "SELECT id, password_hash, user_level FROM users WHERE username=$1", username
    )
    if not row:
        return None
    user_id, password_hash, user_level = row["id"], row["password_hash"], row["user_level"]
    if bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")):
        return {"id": user_id, "username": username, "level": user_level}
    return None


async def get_user_level_name(level: int) -> str:
    return {0: "Staff", 1: "Manager", 2: "Admin"}.get(level, "Unknown")


async def register_user(username: str, password: str, user_level: int = 0) -> bool:
    existing = await Database.fetchrow(
        "SELECT id FROM users WHERE username=$1", username
    )
    if existing:
        return False
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    await Database.execute(
        "INSERT INTO users (username, password_hash, user_level) VALUES ($1, $2, $3)",
        username, password_hash, user_level,
    )
    return True
