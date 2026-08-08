"""FastAPI dependency injections."""
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
import os

from database import Database

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")
bearer_scheme = HTTPBearer(auto_error=False)

SECRET_KEY = os.environ.get("SECRET_KEY", "dpl-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480

LEVEL_CAN_CREATE = {0: [], 1: [0], 2: [0, 1, 2]}
LEVEL_CAN_EDIT = {0: [], 1: [0], 2: [0, 1]}


async def get_current_user(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        username = payload.get("username")
        level = payload.get("level")
        if user_id is None or username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        return {"id": user_id, "username": username, "level": level}
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def require_level(*allowed_levels):
    async def checker(current_user: dict = Depends(get_current_user)):
        if current_user["level"] not in allowed_levels:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return checker


async def get_db():
    return Database
