# app/api/deps.py
import logging
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.database import get_user_collection
from app.core.security import decode_access_token
from app.models.auth_schemas import UserResponse

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


async def get_current_user(token: str = Depends(oauth2_scheme)) -> UserResponse:
    """Dependency that verifies Bearer token and returns authenticated UserResponse."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
        
    user_id: str | None = payload.get("sub")
    if not user_id:
        raise credentials_exception

    try:
        obj_id = ObjectId(user_id)
    except InvalidId:
        raise credentials_exception

    users_col = get_user_collection()
    user = await users_col.find_one({"_id": obj_id})
    if user is None:
        raise credentials_exception

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    return UserResponse(
        id=str(user["_id"]),
        email=user["email"],
        username=user["username"],
        full_name=user.get("full_name"),
        is_active=user.get("is_active", True),
        created_at=user.get("created_at"),
    )
