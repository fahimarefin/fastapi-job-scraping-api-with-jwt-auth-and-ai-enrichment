# app/models/auth_schemas.py
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field


class UserRegisterRequest(BaseModel):
    email: EmailStr = Field(..., description="Valid email address", examples=["user@example.com"])
    username: str = Field(..., min_length=3, max_length=50, description="Unique username", examples=["johndoe"])
    password: str = Field(..., min_length=6, max_length=128, description="Password (min 6 characters)")
    full_name: str | None = Field(None, max_length=100, description="Full name of user", examples=["John Doe"])


class UserLoginRequest(BaseModel):
    username_or_email: str = Field(..., description="Username or email address", examples=["user@example.com"])
    password: str = Field(..., description="User password")


class UserResponse(BaseModel):
    id: str = Field(..., description="MongoDB Document ID as hex string")
    email: EmailStr
    username: str
    full_name: str | None = None
    is_active: bool = True
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int | None = Field(None, description="Token lifespan in seconds, or None if token does not expire")
    user: UserResponse


class TokenPayload(BaseModel):
    sub: str | None = None
    exp: int | None = None
