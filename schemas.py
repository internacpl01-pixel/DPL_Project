"""Pydantic schemas for request/response validation."""
from enum import Enum
from pydantic import BaseModel, Field


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


class PatchUserRequest(BaseModel):
    username: str = Field("", description="Leave empty to keep current username")
    password: str = Field("", description="Leave empty to keep current password")


class AddDataRequest(BaseModel):
    rows: list = Field(..., description="List of row dicts to insert")
