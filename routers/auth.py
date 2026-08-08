"""Auth router — login, register, me, logout."""
from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from schemas import LoginRequest, RegisterRequest
from services.auth import create_access_token, authenticate_user, get_user_level_name, register_user
from dependencies import get_current_user

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    username = form.username.strip()
    password = form.password
    if not username or not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username and password required")

    user = await authenticate_user(username, password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    access_token = create_access_token({
        "sub": str(user["id"]),
        "username": user["username"],
        "level": user["level"],
    })
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "level": user["level"],
            "level_name": await get_user_level_name(user["level"]),
        },
    }


@router.post("/register")
async def register(body: RegisterRequest):
    username = body.username.strip()
    password = body.password
    level = body.level

    if not username or not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username and password required")
    if len(username) < 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username must be at least 3 characters")
    if len(password) < 4:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 4 characters")
    if level not in (0, 1, 2):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid level")

    success = await register_user(username, password, level)
    if success:
        return {"message": f"User '{username}' registered successfully"}
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Username '{username}' already exists")


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "level": current_user["level"],
        "level_name": await get_user_level_name(current_user["level"]),
    }


@router.post("/logout")
async def logout():
    return {"message": "Logged out"}
