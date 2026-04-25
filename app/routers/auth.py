from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import create_access_token, verify_password
from ..models.user import LoginRequest, LoginResponse

router = APIRouter(prefix="/auth", tags=["auth"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, db: DBDep):
    user = await db.users.find_one({"email": body.email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_CREDENTIALS",
                    "message": "Incorrect email or password.",
                }
            },
        )
    token = create_access_token({"sub": user["_id"], "role": user["role"]})
    return LoginResponse(access_token=token)
