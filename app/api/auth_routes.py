# app/api/auth_routes.py
from datetime import datetime, timezone
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.database import get_user_collection
from app.core.security import create_access_token, hash_password, verify_password
from app.models.auth_schemas import (
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
)

logger = logging.getLogger(__name__)
auth_router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


@auth_router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="Create a new user account with hashed password stored in MongoDB.",
)
async def register(body: UserRegisterRequest) -> UserResponse:
    users_col = get_user_collection()
    normalized_email = body.email.lower().strip()
    normalized_username = body.username.strip()

    # Check if email or username already exists
    existing_user = await users_col.find_one({
        "$or": [
            {"email": normalized_email},
            {"username": normalized_username},
        ]
    })
    if existing_user:
        if existing_user.get("email") == normalized_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A user with this email address already exists.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this username already exists.",
        )

    user_doc = {
        "email": normalized_email,
        "username": normalized_username,
        "hashed_password": hash_password(body.password),
        "full_name": body.full_name,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }

    result = await users_col.insert_one(user_doc)
    logger.info("Created new user: %s (id: %s)", normalized_username, result.inserted_id)

    return UserResponse(
        id=str(result.inserted_id),
        email=user_doc["email"],
        username=user_doc["username"],
        full_name=user_doc.get("full_name"),
        is_active=user_doc["is_active"],
        created_at=user_doc["created_at"],
    )


@auth_router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login with JSON body",
    description="Authenticate user with username or email and password, returning JWT access token.",
)
async def login(body: UserLoginRequest) -> TokenResponse:
    users_col = get_user_collection()
    query_identity = body.username_or_email.strip()

    # Find by email (case-insensitive) or username
    user = await users_col.find_one({
        "$or": [
            {"email": query_identity.lower()},
            {"username": query_identity},
        ]
    })

    if not user or not verify_password(body.password, user.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials. Please check your username/email and password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User account is inactive.",
        )

    settings = get_settings()
    user_id_str = str(user["_id"])
    token = create_access_token(data={"sub": user_id_str, "username": user["username"], "email": user["email"]})
    expires_in_seconds = (
        settings.jwt_access_token_expire_minutes * 60
        if settings.jwt_access_token_expire_minutes and settings.jwt_access_token_expire_minutes > 0
        else None
    )

    user_response = UserResponse(
        id=user_id_str,
        email=user["email"],
        username=user["username"],
        full_name=user.get("full_name"),
        is_active=user.get("is_active", True),
        created_at=user.get("created_at"),
    )

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in_seconds,
        user=user_response,
    )


@auth_router.post(
    "/token",
    summary="OAuth2 Password Form Login (for Swagger UI Authorize)",
    description="Standard OAuth2 form login endpoint allowing interactive authorization in Swagger UI (/docs).",
)
async def login_oauth2_form(form_data: OAuth2PasswordRequestForm = Depends()) -> dict:
    users_col = get_user_collection()
    query_identity = form_data.username.strip()

    user = await users_col.find_one({
        "$or": [
            {"email": query_identity.lower()},
            {"username": query_identity},
        ]
    })

    if not user or not verify_password(form_data.password, user.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username/email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User account is inactive.",
        )

    settings = get_settings()
    user_id_str = str(user["_id"])
    token = create_access_token(data={"sub": user_id_str, "username": user["username"], "email": user["email"]})

    response_data: dict[str, Any] = {
        "access_token": token,
        "token_type": "bearer",
    }
    if settings.jwt_access_token_expire_minutes and settings.jwt_access_token_expire_minutes > 0:
        response_data["expires_in"] = settings.jwt_access_token_expire_minutes * 60

    return response_data


@auth_router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current user profile",
    description="Protected route: returns the profile of the currently authenticated user based on JWT token.",
)
async def get_me(current_user: UserResponse = Depends(get_current_user)) -> UserResponse:
    return current_user
