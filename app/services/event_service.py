from datetime import datetime
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from ..core.event_bus import bus
from ..models.common import CameraState, EventSeverity, EventStatus, EventType
from ..models.device import DeviceEventRequest


def _new_event_id() -> str:
    return "evt-" + uuid4().hex[:12]


async def create_system_event(
    db: AsyncIOMotorDatabase,
    container_id: str,
    device_id: str,
    event_type: EventType,
    severity: EventSeverity,
    summary: str,
    state: dict | None = None,
) -> str:
    """Create a backend-generated event (alert rules, offline monitor)."""
    event_id = _new_event_id()
    now = datetime.utcnow()
    doc = {
        "_id": event_id,
        # Use event_id as message_id so the (device_id, message_id) unique
        # sparse index never treats two system events as duplicates.
        "message_id": event_id,
        "container_id": container_id,
        "device_id": device_id,
        "type": event_type.value,
        "severity": severity.value,
        "status": EventStatus.OPEN.value,
        "started_at": now,
        "ended_at": None,
        "summary": summary,
        "state": state or {},
        "evidence": {"media_ids": []},
        "created_at": now,
        "updated_at": now,
    }
    await db.events.insert_one(doc)
    await bus.publish(
        "alarm.created",
        {"event_id": event_id, "container_id": container_id, "device_id": device_id,
         "type": event_type.value, "severity": severity.value, "summary": summary,
         "started_at": now.isoformat() + "Z"},
    )
    return event_id


async def process_device_event(
    db: AsyncIOMotorDatabase, device: dict, payload: DeviceEventRequest
) -> dict:
    message_id = str(payload.message_id)
    now = datetime.utcnow()

    # Dedup: check for existing event with same device_id + message_id
    existing = await db.events.find_one(
        {"device_id": payload.device_id, "message_id": message_id}
    )
    if existing:
        event_id = existing["_id"]
        image_needed = (
            payload.event.type == EventType.GARBAGE_DETECTED.value
            and payload.event.evidence is not None
            and payload.event.evidence.image_available
            and not existing.get("evidence", {}).get("media_ids")
        )
        return {
            "accepted": True,
            "duplicate": True,
            "event_id": event_id,
            "upload_image": image_needed,
            "media_upload_url": f"/api/v1/device/events/{event_id}/media" if image_needed else None,
            "server_time": now.isoformat() + "Z",
        }

    event_id = _new_event_id()
    is_resolution = payload.event.type == EventType.GARBAGE_CLEARED.value
    event_doc = {
        "_id": event_id,
        "message_id": message_id,
        "container_id": payload.container_id,
        "device_id": payload.device_id,
        "type": payload.event.type,
        "severity": payload.event.severity,
        "status": EventStatus.RESOLVED.value if is_resolution else EventStatus.OPEN.value,
        "started_at": payload.event.started_at,
        "ended_at": payload.event.ended_at,
        "confidence": payload.event.confidence,
        "summary": payload.event.summary,
        "state": payload.event.state.model_dump() if payload.event.state else {},
        "evidence": {"media_ids": []},
        "created_at": now,
        "updated_at": now,
    }

    inserted = True
    try:
        await db.events.insert_one(event_doc)
    except DuplicateKeyError:
        inserted = False
        existing = await db.events.find_one(
            {"device_id": payload.device_id, "message_id": message_id}
        )
        event_id = existing["_id"] if existing else event_id

    if inserted and not is_resolution:
        await bus.publish(
            "alarm.created",
            {"event_id": event_id, "container_id": payload.container_id,
             "device_id": payload.device_id, "type": payload.event.type,
             "severity": payload.event.severity, "summary": payload.event.summary,
             "started_at": payload.event.started_at.isoformat() + "Z"},
        )

    # Side effects by event type
    if payload.event.type == EventType.GARBAGE_DETECTED.value:
        await db.containers.update_one(
            {"_id": payload.container_id},
            {
                "$set": {
                    "latest_state.camera_state": CameraState.GARBAGE_DETECTED.value,
                    "updated_at": now,
                }
            },
        )

    elif payload.event.type == EventType.GARBAGE_CLEARED.value:
        result = await db.events.update_many(
            {
                "container_id": payload.container_id,
                "type": EventType.GARBAGE_DETECTED.value,
                "status": EventStatus.OPEN.value,
            },
            {
                "$set": {
                    "status": EventStatus.RESOLVED.value,
                    "ended_at": payload.event.started_at,
                    "updated_at": now,
                }
            },
        )
        await db.containers.update_one(
            {"_id": payload.container_id},
            {
                "$set": {
                    "latest_state.camera_state": CameraState.EVERYTHING_OK.value,
                    "updated_at": now,
                }
            },
        )
        if result.modified_count > 0:
            await bus.publish(
                "alarm.updated",
                {
                    "container_id": payload.container_id,
                    "device_id": payload.device_id,
                    "resolved_type": EventType.GARBAGE_DETECTED.value,
                    "status": EventStatus.RESOLVED.value,
                    "resolution_event_id": event_id,
                    "summary": payload.event.summary,
                    "resolved_at": now.isoformat() + "Z",
                },
            )

    elif payload.event.type == EventType.TAMPER_OPEN.value:
        await db.containers.update_one(
            {"_id": payload.container_id},
            {"$set": {"latest_state.tamper_open": True, "updated_at": now}},
        )

    elif payload.event.type == EventType.TAMPER_CLOSED.value:
        await db.containers.update_one(
            {"_id": payload.container_id},
            {"$set": {"latest_state.tamper_open": False, "updated_at": now}},
        )

    image_needed = (
        payload.event.type == EventType.GARBAGE_DETECTED.value
        and payload.event.evidence is not None
        and payload.event.evidence.image_available
    )

    return {
        "accepted": True,
        "event_id": event_id,
        "upload_image": image_needed,
        "media_upload_url": f"/api/v1/device/events/{event_id}/media" if image_needed else None,
        "server_time": now.isoformat() + "Z",
    }
