# app/core/security.py
from datetime import datetime, timedelta, timezone
from typing import Any
import bcrypt
import jwt

from app.core.config import get_settings


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt salt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plaintext password against bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        return False


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token.
    
    If expires_delta is provided or jwt_access_token_expire_minutes > 0, an 'exp' claim is added.
    Otherwise, if jwt_access_token_expire_minutes is 0 or None, the token does not expire.
    """
    settings = get_settings()
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    to_encode["iat"] = now
    
    if expires_delta is not None:
        to_encode["exp"] = now + expires_delta
    elif settings.jwt_access_token_expire_minutes and settings.jwt_access_token_expire_minutes > 0:
        to_encode["exp"] = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT access token."""
    settings = get_settings()
    decode_options = {}
    if not settings.jwt_access_token_expire_minutes or settings.jwt_access_token_expire_minutes <= 0:
        decode_options["verify_exp"] = False

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options=decode_options,
        )
        return payload
    except jwt.PyJWTError:
        return None
