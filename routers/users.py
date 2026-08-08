"""Users router — list, add, edit, delete, change level."""
from fastapi import APIRouter, Depends, HTTPException, status

from dependencies import get_current_user, require_level, LEVEL_CAN_EDIT, LEVEL_CAN_CREATE
from schemas import AddUserRequest, UpdateLevelRequest, PatchUserRequest
from services.auth import get_user_level_name, register_user
from services.users import (
    list_users,
    edit_user,
    delete_user,
    update_user_level,
    get_user_level,
)

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users")
async def list_users_endpoint(current_user: dict = Depends(require_level(1, 2))):
    return await list_users()


@router.post("/users")
async def add_user(body: AddUserRequest, current_user: dict = Depends(require_level(1, 2))):
    username = body.username.strip()
    password = body.password
    level = body.level

    current_level = current_user["level"]
    allowed_levels = LEVEL_CAN_CREATE.get(current_level, [])
    if level not in allowed_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have permission to create a {await get_user_level_name(level)} user",
        )
    if not username or not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username and password required")
    if len(username) < 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username must be at least 3 characters")
    if len(password) < 4:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 4 characters")

    success = await register_user(username, password, level)
    if success:
        return {"message": f"User '{username}' created successfully"}
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Username '{username}' already exists")


@router.put("/users/{user_id}/level")
async def update_user_level_endpoint(user_id: int, body: UpdateLevelRequest, current_user: dict = Depends(require_level(2))):
    new_level = body.level
    if new_level not in (0, 1, 2):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid level")

    success = await update_user_level(user_id, new_level)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return {"message": f"User level updated to {await get_user_level_name(new_level)}"}


@router.patch("/users/{user_id}")
async def edit_user_endpoint(user_id: int, body: PatchUserRequest, current_user: dict = Depends(get_current_user)):
    current_level = current_user["level"]
    target_levels = LEVEL_CAN_EDIT.get(current_level, [])

    target_level = await get_user_level(user_id)
    if target_level is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target_level not in target_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot edit a {await get_user_level_name(target_level)} user",
        )
    if user_id == current_user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot edit your own account")

    username = (body.username or "").strip()
    password = (body.password or "").strip()
    if not username and not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide at least username or password to update")

    if username and len(username) < 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username must be at least 3 characters")
    if password and len(password) < 4:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 4 characters")

    kwargs = {}
    if username:
        kwargs["username"] = username
    if password:
        kwargs["password"] = password

    success = await edit_user(user_id, **kwargs)
    if success:
        changes = [k for k in kwargs]
        return {"message": f"Updated {', '.join(changes)} successfully"}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to update user")


@router.delete("/users/{user_id}")
async def delete_user_endpoint(user_id: int, current_user: dict = Depends(get_current_user)):
    current_level = current_user["level"]
    target_levels = LEVEL_CAN_EDIT.get(current_level, [])

    target_level = await get_user_level(user_id)
    if target_level is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target_level not in target_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot delete a {await get_user_level_name(target_level)} user",
        )
    if user_id == current_user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete your own account")

    deleted_name = await delete_user(user_id)
    if deleted_name:
        return {"message": f"User '{deleted_name}' deleted successfully"}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to delete user")
