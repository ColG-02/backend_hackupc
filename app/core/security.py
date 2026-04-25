from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorDatabase
from passlib.context import CryptContext

from .config import settings
from .database import get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: DBDep,
) -> dict:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": {"code": "UNAUTHORIZED", "message": "Invalid or expired token."}},
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise exc
    except JWTError:
        raise exc

    user = await db.users.find_one({"_id": user_id})
    if not user:
        raise exc
    return user


async def get_current_device(
    authorization: Annotated[str | None, Header()] = None,
    x_device_id: Annotated[str | None, Header()] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    if not authorization or not authorization.startswith("DeviceToken "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_DEVICE_TOKEN",
                    "message": "Authorization: DeviceToken <token> header is required.",
                }
            },
        )
    if not x_device_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "X-Device-Id header is required.",
                }
            },
        )

    token = authorization.removeprefix("DeviceToken ").strip()
    device = await db.devices.find_one({"_id": x_device_id})
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "DEVICE_NOT_FOUND", "message": "Device not found."}},
        )
    if not verify_password(token, device["device_token_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_DEVICE_TOKEN",
                    "message": "Device token is invalid.",
                }
            },
        )
    if device.get("status") == "DISABLED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "DEVICE_DISABLED", "message": "Device is disabled."}},
        )
    return device


def require_roles(*roles: str):
    """Returns a FastAPI dependency that enforces role membership."""

    async def _check(
        current_user: Annotated[dict, Depends(get_current_user)],
    ) -> dict:
        if current_user.get("role") not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "FORBIDDEN",
                        "message": "You do not have permission to perform this action.",
                    }
                },
            )
        return current_user

    return _check
