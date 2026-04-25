from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user
from ..models.media import MediaOut

router = APIRouter(prefix="/media", tags=["media"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]


@router.get("/{media_id}", response_model=MediaOut)
async def get_media_metadata(media_id: str, db: DBDep, _user: UserDep):
    doc = await db.media.find_one({"_id": media_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "MEDIA_NOT_FOUND", "message": "Media not found."}},
        )
    return MediaOut(
        media_id=doc["_id"],
        event_id=doc.get("event_id", ""),
        container_id=doc.get("container_id", ""),
        device_id=doc.get("device_id", ""),
        content_type=doc.get("content_type", "image/jpeg"),
        captured_at=doc.get("captured_at"),
        width=doc.get("width"),
        height=doc.get("height"),
        model_id=doc.get("model_id"),
        camera_state=doc.get("camera_state"),
        confidence=doc.get("confidence"),
        url=f"/api/v1/media/{media_id}/file",
    )


@router.get("/{media_id}/file")
async def get_media_file(media_id: str, db: DBDep, _user: UserDep):
    doc = await db.media.find_one({"_id": media_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "MEDIA_NOT_FOUND", "message": "Media not found."}},
        )
    path = doc.get("storage_path", "")
    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "MEDIA_NOT_FOUND", "message": "File not found on disk."}},
        )
    return FileResponse(path, media_type="image/jpeg")
