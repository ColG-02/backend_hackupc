import json
import secrets
from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_device, hash_password
from ..models.device import (
    BootstrapRequest,
    BootstrapResponse,
    ConfigAckRequest,
    ConfigAckResponse,
    ConfigResponse,
    DeviceConfig,
    DeviceEventRequest,
    DeviceEventResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    TelemetryBatchRequest,
    TelemetryResponse,
)
from ..models.common import DeviceStatus, EventSeverity, EventType
from ..services.event_service import create_system_event, process_device_event
from ..services.media_service import save_event_image
from ..services.telemetry import process_telemetry_batch

router = APIRouter(prefix="/device", tags=["device-ingest"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
DeviceDep = Annotated[dict, Depends(get_current_device)]


def _new_device_id(seq: int) -> str:
    return f"cont-{seq:06d}"


def _utcnow_str() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ── Bootstrap ─────────────────────────────────────────────────────────────────

@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(body: BootstrapRequest, db: DBDep):
    if body.schema_version.split(".")[0] != "1":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_SCHEMA_VERSION",
                    "message": "Unsupported schema version.",
                }
            },
        )

    # Validate claim code
    claim = await db.claim_codes.find_one({"code": body.claim_code})
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_CLAIM_CODE",
                    "message": "The provided claim code is not valid.",
                }
            },
        )
    if claim.get("used"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "DEVICE_ALREADY_CLAIMED",
                    "message": "This claim code has already been used.",
                }
            },
        )

    # Check factory_device_id not already registered
    existing = await db.devices.find_one({"factory_device_id": body.factory_device_id})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "DEVICE_ALREADY_CLAIMED",
                    "message": "This factory device ID has already been claimed.",
                }
            },
        )

    # Generate device_id using a counter
    counter = await db.counters.find_one_and_update(
        {"_id": "device_seq"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    device_id = _new_device_id(counter["seq"])
    container_id = claim["container_id"]

    # Generate token and store hash
    plain_token = secrets.token_urlsafe(32)
    token_hash = hash_password(plain_token)

    now = datetime.utcnow()
    await db.devices.insert_one(
        {
            "_id": device_id,
            "factory_device_id": body.factory_device_id,
            "container_id": container_id,
            "device_token_hash": token_hash,
            "status": DeviceStatus.ONLINE.value,
            "last_seen_at": now,
            "firmware": body.firmware.model_dump(),
            "capabilities": body.capabilities.model_dump(),
            "created_at": now,
            "updated_at": now,
        }
    )

    # Link device to container
    await db.containers.update_one(
        {"_id": container_id},
        {"$set": {"device_id": device_id, "updated_at": now}},
    )

    # Mark claim code used
    await db.claim_codes.update_one(
        {"_id": claim["_id"]},
        {"$set": {"used": True, "used_by_device": device_id, "used_at": now}},
    )

    # Fetch container config
    container = await db.containers.find_one({"_id": container_id})
    config_revision = container.get("config_revision", 1) if container else 1
    raw_config = container.get("config") if container else None
    device_config = DeviceConfig(**raw_config) if raw_config else DeviceConfig()

    return BootstrapResponse(
        device_id=device_id,
        container_id=container_id,
        device_token=plain_token,
        server_time=_utcnow_str(),
        config_revision=config_revision,
        config=device_config,
    )


# ── Telemetry ─────────────────────────────────────────────────────────────────

@router.post("/telemetry", response_model=TelemetryResponse)
async def upload_telemetry(
    body: TelemetryBatchRequest, db: DBDep, device: DeviceDep
):
    if not body.readings:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_TELEMETRY",
                    "message": "readings must contain at least one reading.",
                }
            },
        )
    if len(body.readings) > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_TELEMETRY",
                    "message": "readings batch size must not exceed 100.",
                }
            },
        )
    result = await process_telemetry_batch(db, device, body)
    return TelemetryResponse(**result)


# ── Events ────────────────────────────────────────────────────────────────────

@router.post("/events", response_model=DeviceEventResponse, status_code=201)
async def upload_event(
    body: DeviceEventRequest, db: DBDep, device: DeviceDep
):
    result = await process_device_event(db, device, body)
    return DeviceEventResponse(
        event_id=result["event_id"],
        upload_image=result["upload_image"],
        media_upload_url=result.get("media_upload_url"),
        server_time=result["server_time"],
    )


# ── Event image upload ────────────────────────────────────────────────────────

@router.post("/events/{event_id}/media", status_code=201)
async def upload_event_media(
    event_id: str,
    image: UploadFile,
    metadata: str,
    db: DBDep,
    device: DeviceDep,
):
    event = await db.events.find_one({"_id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "EVENT_NOT_FOUND", "message": "Event not found."}},
        )

    try:
        meta_dict = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "metadata field must be valid JSON.",
                }
            },
        )

    return await save_event_image(db, event_id, image, meta_dict)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(body: HeartbeatRequest, db: DBDep, device: DeviceDep):
    now = datetime.utcnow()
    was_offline = device.get("status") == DeviceStatus.OFFLINE.value

    await db.devices.update_one(
        {"_id": body.device_id},
        {
            "$set": {
                "status": DeviceStatus.ONLINE.value,
                "last_seen_at": now,
                "firmware": body.firmware.model_dump(),
                "updated_at": now,
            }
        },
    )

    if was_offline:
        container = await db.containers.find_one({"_id": body.container_id})
        if container:
            await create_system_event(
                db,
                body.container_id,
                body.device_id,
                EventType.DEVICE_ONLINE,
                EventSeverity.INFO,
                f"Device {body.device_id} reconnected.",
            )

    container = await db.containers.find_one({"_id": body.container_id})
    config_revision = container.get("config_revision", 1) if container else 1

    return HeartbeatResponse(
        server_time=_utcnow_str(),
        config_revision=config_revision,
    )


# ── Config poll ───────────────────────────────────────────────────────────────

@router.get("/config", response_model=ConfigResponse)
async def get_config(known_revision: int, db: DBDep, device: DeviceDep):
    container_id = device.get("container_id")
    container = await db.containers.find_one({"_id": container_id}) if container_id else None
    config_revision = container.get("config_revision", 1) if container else 1

    if config_revision <= known_revision:
        return ConfigResponse(
            changed=False,
            config_revision=config_revision,
            server_time=_utcnow_str(),
        )

    raw_config = container.get("config") if container else None
    device_config = DeviceConfig(**raw_config) if raw_config else DeviceConfig()
    return ConfigResponse(
        changed=True,
        config_revision=config_revision,
        server_time=_utcnow_str(),
        config=device_config,
    )


# ── Config ack ────────────────────────────────────────────────────────────────

@router.post("/config/ack", response_model=ConfigAckResponse)
async def ack_config(body: ConfigAckRequest, db: DBDep, device: DeviceDep):
    await db.devices.update_one(
        {"_id": body.device_id},
        {
            "$set": {
                "last_config_ack_revision": body.config_revision,
                "last_config_ack_at": body.applied_at,
                "last_config_ack_success": body.success,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return ConfigAckResponse(config_revision=body.config_revision)
