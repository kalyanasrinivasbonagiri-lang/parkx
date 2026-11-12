"""Password hashing, JWT creation, and authenticated role dependencies."""

from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database

from app.config import get_settings
from app.db import get_database


password_hash = PasswordHash.recommended()
bearer_scheme = HTTPBearer()


def hash_password(password: str) -> str:
    """Return a secure one-way hash for a plain-text password."""
    return password_hash.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a submitted password against its stored hash."""
    return password_hash.verify(password, stored_hash)


def create_access_token(user_id: str, role: str) -> str:
    """Sign a short-lived JWT containing the account id and role."""
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    claims = {"sub": user_id, "role": role, "exp": expires_at}
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Validate the bearer token and load its user from MongoDB."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        settings = get_settings()
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = claims.get("sub")
        if not isinstance(user_id, str):
            raise credentials_error
    except InvalidTokenError as error:
        raise credentials_error from error

    try:
        account_id = ObjectId(user_id)
    except InvalidId as error:
        raise credentials_error from error

    user = database.users.find_one({"_id": account_id, "is_active": True})
    if user is None:
        raise credentials_error
    return user


def require_roles(*allowed_roles: str) -> Callable[..., dict[str, Any]]:
    """Build a dependency that allows only the listed account roles."""
    def check_role(
        user: dict[str, Any] = Depends(get_current_user),
    ) -> dict[str, Any]:
        if user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource",
            )
        return user

    return check_role

