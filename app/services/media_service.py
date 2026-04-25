import os
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.config import settings


def _new_media_id() -> str:
    return "media-" + uuid4().hex[:8]


async def save_event_image(
    db: AsyncIOMotorDatabase,
    event_id: str,
    image: UploadFile,
    metadata: dict,
) -> dict:
    media_id = _new_media_id()
    now = datetime.utcnow()

    # Validate size
    content = await image.read()
    max_bytes = settings.MAX_IMAGE_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": {
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": f"Image exceeds {settings.MAX_IMAGE_SIZE_MB} MB limit.",
                }
            },
        )

    # Save to disk
    event_dir = os.path.join(settings.UPLOAD_DIR, "events", event_id)
    os.makedirs(event_dir, exist_ok=True)
    file_path = os.path.join(event_dir, f"{media_id}.jpg")
    with open(file_path, "wb") as f:
        f.write(content)

    storage_path = f"uploads/events/{event_id}/{media_id}.jpg"

    media_doc = {
        "_id": media_id,
        "event_id": event_id,
        "container_id": metadata.get("container_id", ""),
        "device_id": metadata.get("device_id", ""),
        "storage_type": "local",
        "storage_path": storage_path,
        "content_type": "image/jpeg",
        "captured_at": metadata.get("captured_at"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "model_id": metadata.get("model_id"),
        "camera_state": metadata.get("camera_state"),
        "confidence": metadata.get("confidence"),
        "privacy_processed": metadata.get("privacy_processed", False),
        "created_at": now,
    }
    await db.media.insert_one(media_doc)

    # Attach media_id to the event
    await db.events.update_one(
        {"_id": event_id},
        {"$addToSet": {"evidence.media_ids": media_id}, "$set": {"updated_at": now}},
    )

    return {
        "accepted": True,
        "media_id": media_id,
        "event_id": event_id,
        "stored": True,
    }


def get_media_file_path(storage_path: str) -> str:
    return storage_path
