from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from jose import JWTError, jwt
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import sys
import os
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_connection, create_database
from models import (
    create_tables,
    insert_default_mappings,
    create_users_table,
    create_default_admin,
    register_user,
    add_custom_field,
    get_next_field_number,
    log_field_change,
    get_user_level_name,
    update_user_by_id,
    delete_user_by_id,
)
from web_helpers import (
    get_field_mappings,
    get_table_structure,
    get_change_log,
    get_all_users_data,
    update_field_mapping,
    change_user_level_by_id,
)


SECRET_KEY = os.environ.get("SECRET_KEY", "dpl-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480

# OAuth2 scheme — tells Swagger UI to show username/password login form
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")
# HTTPBearer for manual token input (fallback)
bearer_scheme = HTTPBearer(auto_error=False)

LEVEL_CAN_CREATE = {
    0: [],
    1: [0],
    2: [0, 1, 2],
}

LEVEL_CAN_EDIT = {
    0: [],
    1: [0],
    2: [0, 1],
}


# Enum types — show as dropdowns in Swagger UI
class FieldType(str, Enum):
    date = "date"
    num = "num"
    text = "text"


class UserLevel(int, Enum):
    staff = 0
    manager = 1
    admin = 2


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=4)
    level: UserLevel = UserLevel.staff


class UpdateMappingRequest(BaseModel):
    displayname: str = Field("", description="Leave empty to keep current display name")
    mapfields: str = Field("", description="Comma-separated values to append")


class CustomFieldRequest(BaseModel):
    type: FieldType


class AddUserRequest(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=4)
    level: UserLevel = UserLevel.staff


class UpdateLevelRequest(BaseModel):
    level: UserLevel


class EditUserRequest(BaseModel):
    username: str = Field("", description="Leave empty to keep current username")
    password: str = Field("", description="Leave empty to keep current password")


class PatchUserRequest(BaseModel):
    username: str = Field("", description="Leave empty to keep current username")
    password: str = Field("", description="Leave empty to keep current password")


app = FastAPI(title="DPL Data Bank API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
FRONTEND_DIR = os.path.normpath(FRONTEND_DIR)
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="frontend-static")


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        username = payload.get("username")
        level = payload.get("level")
        if user_id is None or username is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return {"id": user_id, "username": username, "level": level}
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )


def require_level(*allowed_levels):
    async def checker(current_user: dict = Depends(get_current_user)):
        if current_user["level"] not in allowed_levels:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user
    return checker


@app.on_event("startup")
def startup():
    create_database()
    create_tables()
    insert_default_mappings()
    create_users_table()
    create_default_admin()


@app.get("/", include_in_schema=False)
def serve_index():
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "DPL Data Bank API is running."}


@app.get("/frontend/", include_in_schema=False)
def serve_frontend_index():
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Frontend not found")


@app.get("/frontend/{path:path}", include_in_schema=False)
def serve_frontend_assets(path: str):
    file_path = os.path.join(FRONTEND_DIR, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Not found")


@app.post("/api/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    username = form.username.strip()
    password = form.password

    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username and password required",
        )

    from db import authenticate_user
    user = authenticate_user(username, password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

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
            "level_name": get_user_level_name(user["level"]),
        },
    }


@app.post("/api/register")
def register(body: RegisterRequest):
    username = body.username.strip()
    password = body.password
    level = body.level

    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username and password required",
        )

    if len(username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be at least 3 characters",
        )

    if len(password) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 4 characters",
        )

    if level not in (0, 1, 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid level",
        )

    success = register_user(username, password, level)

    if success:
        return {"message": f"User '{username}' registered successfully"}

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Username '{username}' already exists",
    )


@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "level": current_user["level"],
        "level_name": get_user_level_name(current_user["level"]),
    }


@app.post("/api/logout")
async def logout():
    return {"message": "Logged out"}


@app.get("/api/field-mappings")
async def list_field_mappings(current_user: dict = Depends(get_current_user)):
    return get_field_mappings()


@app.put("/api/field-mappings/{fieldname}")
def update_mapping(fieldname: str, body: UpdateMappingRequest,
                   current_user: dict = Depends(require_level(1, 2))):
    displayname = body.displayname.strip()
    mapfields = body.mapfields.strip()

    conn = get_connection()
    cursor = conn.cursor()

    # Check if fieldname already exists — if not, create it (upsert)
    cursor.execute(
        "SELECT id, displayname, mapfields FROM fieldmap WHERE fieldname=%s",
        (fieldname,),
    )
    record = cursor.fetchone()

    if not record:
        # Auto-create the mapping if it doesn't exist
        new_displayname = displayname.strip() if displayname.strip() else fieldname
        cursor.execute(
            "INSERT INTO fieldmap (fieldname, displayname, mapfields) VALUES (%s, %s, %s)",
            (fieldname, new_displayname, ""),
        )
        conn.commit()
        record = (cursor.lastrowid, new_displayname, "")
    record_id, existing_displayname, existing_mapfields = record

    # If displayname is empty, keep the existing value from DB
    if not displayname:
        displayname = existing_displayname

    if mapfields:
        new_items = [
            item.strip()
            for item in mapfields.split(",")
            if item.strip()
        ]
        # Check duplicates within same row
        existing_items = [
            item.strip()
            for item in existing_mapfields.split(",")
            if item.strip()
        ]
        duplicates_in_row = [item for item in new_items if item in existing_items]
        if duplicates_in_row:
            cursor.close()
            conn.close()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate values in same row: {', '.join(duplicates_in_row)}",
            )
        # Check duplicates across OTHER rows
        all_mappings = get_field_mappings()
        all_values_in_other_rows = set()
        for m in all_mappings:
            if m["fieldname"] == fieldname:
                continue
            for v in m["mapfields"].split(","):
                v = v.strip()
                if v:
                    all_values_in_other_rows.add(v.lower())
        duplicates_across = [item for item in new_items if item.lower() in all_values_in_other_rows]
        if duplicates_across:
            cursor.close()
            conn.close()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Values already exist in another mapping: {', '.join(duplicates_across)}",
            )
        mapfields = existing_mapfields + ", " + ", ".join(new_items)
    else:
        mapfields = existing_mapfields

    cursor.close()
    conn.close()

    update_field_mapping(fieldname, displayname, mapfields)

    return {"message": "Mapping updated successfully"}


@app.delete("/api/field-mappings/{fieldname}/mapfield")
async def delete_mapfield(fieldname: str, request: Request,
                    current_user: dict = Depends(require_level(1, 2))):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    value_to_remove = (body.get("value") or "").strip()

    if not value_to_remove:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Value to remove is required",
        )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, displayname, mapfields FROM fieldmap WHERE fieldname=%s",
        (fieldname,),
    )
    record = cursor.fetchone()

    if not record:
        cursor.close()
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Field not found",
        )

    record_id, existing_displayname, existing_mapfields = record

    items = [item.strip() for item in existing_mapfields.split(",") if item.strip()]

    if value_to_remove not in items:
        cursor.close()
        conn.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Value '{value_to_remove}' not found in mapfields",
        )

    items = [item for item in items if item != value_to_remove]
    new_mapfields = ", ".join(items)

    cursor.close()
    conn.close()

    update_field_mapping(fieldname, existing_displayname, new_mapfields)

    return {"message": f"Removed '{value_to_remove}' from mapfields"}


@app.get("/api/table-structure")
def list_table_structure(current_user: dict = Depends(get_current_user)):
    return get_table_structure()


@app.get("/api/change-log")
def list_change_log(current_user: dict = Depends(require_level(1, 2))):
    return get_change_log()


@app.post("/api/custom-fields")
def create_custom_field(body: CustomFieldRequest,
                         current_user: dict = Depends(get_current_user)):
    field_type = body.type.strip().lower()

    if field_type not in ("date", "num", "text"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid type. Use date, num, or text",
        )

    conn = get_connection()
    next_num = get_next_field_number(conn, field_type)

    type_map = {
        "date": ("DATE", "field_date"),
        "num": ("REAL", "field_num"),
        "text": ("TEXT", "field_text"),
    }

    sql_type, prefix = type_map[field_type]
    col_name = f"{prefix}_{next_num}"

    cursor = conn.cursor()
    cursor.execute(
        f"ALTER TABLE master ADD COLUMN IF NOT EXISTS {col_name} {sql_type}",
    )
    conn.commit()
    cursor.close()
    conn.close()

    return {"column": col_name, "type": sql_type}


@app.get("/api/users")
def list_users(current_user: dict = Depends(require_level(1, 2))):
    return get_all_users_data()


@app.post("/api/users")
def add_user(body: AddUserRequest,
             current_user: dict = Depends(require_level(1, 2))):
    username = body.username.strip()
    password = body.password
    level = body.level

    current_level = current_user["level"]
    allowed_levels = LEVEL_CAN_CREATE.get(current_level, [])

    if level not in allowed_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have permission to create a {get_user_level_name(level)} user",
        )

    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username and password required",
        )

    if len(username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be at least 3 characters",
        )

    if len(password) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 4 characters",
        )

    success = register_user(username, password, level)

    if success:
        return {"message": f"User '{username}' created successfully"}

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Username '{username}' already exists",
    )


@app.put("/api/users/{user_id}/level")
def update_user_level(user_id: int, body: UpdateLevelRequest,
                      current_user: dict = Depends(require_level(2))):
    new_level = body.level

    if new_level not in (0, 1, 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid level",
        )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT username FROM users WHERE id=%s", (user_id,))
    record = cursor.fetchone()

    cursor.close()
    conn.close()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    change_user_level_by_id(user_id, new_level)

    return {"message": f"User level updated to {get_user_level_name(new_level)}"}


@app.patch("/api/users/{user_id}")
def edit_user(user_id: int, body: PatchUserRequest,
              current_user: dict = Depends(get_current_user)):
    current_level = current_user["level"]
    target_levels = LEVEL_CAN_EDIT.get(current_level, [])

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT user_level FROM users WHERE id=%s", (user_id,))
    record = cursor.fetchone()
    cursor.close()
    conn.close()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    target_level = record[0]

    if target_level not in target_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot edit a {get_user_level_name(target_level)} user",
        )

    if user_id == current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot edit your own account",
        )

    username = (body.username or "").strip()
    password = (body.password or "").strip()

    if not username and not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least username or password to update",
        )

    kwargs = {}

    if username:
        if len(username) < 3:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username must be at least 3 characters",
            )
        kwargs["username"] = username

    if password:
        if len(password) < 4:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password must be at least 4 characters",
            )
        kwargs["password"] = password

    success = update_user_by_id(user_id, **kwargs)

    if success:
        changes = []
        if "username" in kwargs:
            changes.append("username")
        if "password" in kwargs:
            changes.append("password")
        return {"message": f"Updated {', '.join(changes)} successfully"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Failed to update user",
    )


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int,
                current_user: dict = Depends(get_current_user)):
    current_level = current_user["level"]
    target_levels = LEVEL_CAN_EDIT.get(current_level, [])

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT username, user_level FROM users WHERE id=%s", (user_id,))
    record = cursor.fetchone()
    cursor.close()
    conn.close()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    target_username, target_level = record

    if target_level not in target_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot delete a {get_user_level_name(target_level)} user",
        )

    if user_id == current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot delete your own account",
        )

    deleted = delete_user_by_id(user_id)

    if deleted:
        return {"message": f"User '{target_username}' deleted successfully"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Failed to delete user",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_api:app", host="0.0.0.0", port=5000, reload=True)
