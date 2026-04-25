import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user, hash_password, require_roles
from ..models.device import (
    AssignDeviceRequest,
    AssignDeviceResponse,
    ClaimCodeResponse,
    CreateClaimCodeRequest,
    DeviceListResponse,
    DeviceOut,
    UpdateDeviceConfigRequest,
    UpdateDeviceConfigResponse,
)

router = APIRouter(prefix="/devices", tags=["devices"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
AdminDep = Annotated[dict, Depends(require_roles("ADMIN"))]


def _doc_to_out(doc: dict) -> DeviceOut:
    health = None
    # Carry health snapshot if stored on device doc
    if any(k in doc for k in ("rssi_dbm", "uptime_sec", "free_disk_mb", "offline_queue_count")):
        health = {
            "rssi_dbm": doc.get("rssi_dbm"),
            "uptime_sec": doc.get("uptime_sec"),
            "free_disk_mb": doc.get("free_disk_mb"),
            "offline_queue_count": doc.get("offline_queue_count", 0),
        }
    return DeviceOut(
        device_id=doc["_id"],
        container_id=doc.get("container_id"),
        status=doc.get("status", "UNKNOWN"),
        last_seen_at=doc.get("last_seen_at"),
        created_at=doc.get("created_at"),
        firmware=doc.get("firmware"),
        health=health,
    )


@router.get("", response_model=DeviceListResponse)
async def list_devices(db: DBDep, _user: UserDep):
    docs = await db.devices.find().to_list(None)
    return DeviceListResponse(items=[_doc_to_out(d) for d in docs], total=len(docs))


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: str, db: DBDep, _user: UserDep):
    doc = await db.devices.find_one({"_id": device_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "DEVICE_NOT_FOUND", "message": "Device not found."}},
        )
    return _doc_to_out(doc)


@router.post("/{device_id}/assign", response_model=AssignDeviceResponse)
async def assign_device(
    device_id: str, body: AssignDeviceRequest, db: DBDep, _admin: AdminDep
):
    device = await db.devices.find_one({"_id": device_id})
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "DEVICE_NOT_FOUND", "message": "Device not found."}},
        )
    container = await db.containers.find_one({"_id": body.container_id})
    if not container:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CONTAINER_NOT_FOUND",
                    "message": "Container not found.",
                }
            },
        )
    now = datetime.utcnow()
    # Clear device_id from whatever container currently owns this device
    old_container_id = device.get("container_id")
    if old_container_id and old_container_id != body.container_id:
        await db.containers.update_one(
            {"_id": old_container_id},
            {"$set": {"device_id": None, "updated_at": now}},
        )
    await db.devices.update_one(
        {"_id": device_id},
        {"$set": {"container_id": body.container_id, "updated_at": now}},
    )
    await db.containers.update_one(
        {"_id": body.container_id},
        {"$set": {"device_id": device_id, "updated_at": now}},
    )
    return AssignDeviceResponse(device_id=device_id, container_id=body.container_id)


@router.patch("/{device_id}/config", response_model=UpdateDeviceConfigResponse)
async def update_device_config(
    device_id: str, body: UpdateDeviceConfigRequest, db: DBDep, _admin: AdminDep
):
    device = await db.devices.find_one({"_id": device_id})
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "DEVICE_NOT_FOUND", "message": "Device not found."}},
        )
    container_id = device.get("container_id")
    if not container_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Device is not assigned to a container.",
                }
            },
        )

    container = await db.containers.find_one({"_id": container_id})
    existing_config = container.get("config", {}) if container else {}

    patch = body.model_dump(exclude_none=True)
    for key, value in patch.items():
        if isinstance(value, dict):
            if key not in existing_config:
                existing_config[key] = {}
            existing_config[key].update(value)
        else:
            existing_config[key] = value

    current_revision = container.get("config_revision", 1) if container else 1
    new_revision = current_revision + 1

    await db.containers.update_one(
        {"_id": container_id},
        {
            "$set": {
                "config": existing_config,
                "config_revision": new_revision,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return UpdateDeviceConfigResponse(device_id=device_id, config_revision=new_revision)


# ── Claim codes ───────────────────────────────────────────────────────────────

@router.post("/claim-codes", response_model=ClaimCodeResponse, status_code=201)
async def create_claim_code(body: CreateClaimCodeRequest, db: DBDep, _admin: AdminDep):
    container = await db.containers.find_one({"_id": body.container_id})
    if not container:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CONTAINER_NOT_FOUND",
                    "message": "Container not found.",
                }
            },
        )
    code = body.code or secrets.token_hex(8).upper()
    await db.claim_codes.insert_one(
        {
            "code": code,
            "container_id": body.container_id,
            "used": False,
            "created_at": datetime.utcnow(),
        }
    )
    return ClaimCodeResponse(code=code, container_id=body.container_id)
