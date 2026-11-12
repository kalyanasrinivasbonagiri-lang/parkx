"""Driver registration and login routes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.db import get_database


router = APIRouter(prefix="/api/auth", tags=["authentication"])


class DriverRegistration(BaseModel):
    """Fields a driver submits when creating an account."""

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(min_length=7, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    vehicles: list[str] = Field(default_factory=list)

    @field_validator("vehicles")
    @classmethod
    def clean_vehicles(cls, vehicles: list[str]) -> list[str]:
        """Trim and remove empty vehicle numbers supplied in the request."""
        return [vehicle.strip().upper() for vehicle in vehicles if vehicle.strip()]


class LoginRequest(BaseModel):
    """Email and password used to sign in."""

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """JWT response returned after successful registration or login."""

    access_token: str
    token_type: str = "bearer"


def normalize_email(email: str) -> str:
    """Normalize email for case-insensitive uniqueness and lookup."""
    return email.strip().lower()


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_driver(
    request: DriverRegistration,
    database: Database[dict[str, Any]] = Depends(get_database),
) -> TokenResponse:
    """Create a driver account; public registration cannot set a privileged role."""
    email = normalize_email(str(request.email))
    user = {
        "name": request.name.strip(),
        "email": email,
        "phone": request.phone.strip(),
        "password_hash": hash_password(request.password),
        "vehicles": request.vehicles,
        "role": "driver",
        "is_active": True,
    }
    try:
        result = database.users.insert_one(user)
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from error

    token = create_access_token(str(result.inserted_id), user["role"])
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(
    request: LoginRequest,
    database: Database[dict[str, Any]] = Depends(get_database),
) -> TokenResponse:
    """Authenticate a driver, staff member, or admin and return a JWT."""
    email = normalize_email(str(request.email))
    user = database.users.find_one({"email": email, "is_active": True})
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(str(user["_id"]), user["role"])
    return TokenResponse(access_token=token)


@router.get("/me")
def read_current_user(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return safe account details for the signed-in user."""
    response: dict[str, Any] = {
        "id": str(user["_id"]),
        "name": user["name"],
        "email": user["email"],
        "phone": user.get("phone"),
        "role": user["role"],
    }
    if user["role"] == "driver":
        response["vehicles"] = user.get("vehicles", [])
    if user["role"] == "staff":
        assigned_lot_id = user.get("assigned_lot_id")
        response["assigned_lot_id"] = (
            str(assigned_lot_id) if assigned_lot_id is not None else None
        )
    return response

