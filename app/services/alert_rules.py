from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..models.common import CameraState, EventSeverity, EventStatus, EventType
from ..models.device import TelemetryReading
from .event_service import create_system_event


async def run_alert_rules(
    db: AsyncIOMotorDatabase,
    container_id: str,
    device_id: str,
    reading: TelemetryReading,
) -> None:
    await _check_fill_alerts(db, container_id, device_id, reading)
    await _check_camera_alerts(db, container_id, device_id, reading)
    await _check_sensor_fault(db, container_id, device_id, reading)


async def _open_event_exists(
    db: AsyncIOMotorDatabase, container_id: str, event_type: EventType
) -> bool:
    doc = await db.events.find_one(
        {
            "container_id": container_id,
            "type": event_type.value,
            "status": EventStatus.OPEN.value,
        }
    )
    return doc is not None


async def _check_fill_alerts(
    db: AsyncIOMotorDatabase,
    container_id: str,
    device_id: str,
    reading: TelemetryReading,
) -> None:
    fused = reading.fill.fused_pct

    # CRITICAL_FULL — single reading >= 95%
    if fused >= 95:
        if not await _open_event_exists(db, container_id, EventType.CRITICAL_FULL):
            await create_system_event(
                db,
                container_id,
                device_id,
                EventType.CRITICAL_FULL,
                EventSeverity.CRITICAL,
                f"Container fill reached critical level: {fused:.0f}%.",
                {"fused_fill_pct": fused, "fill_state": reading.fill.state.value},
            )
        return  # CRITICAL supersedes FULL_THRESHOLD

    # FULL_THRESHOLD — two consecutive readings >= 85%
    if fused >= 85:
        if not await _open_event_exists(db, container_id, EventType.FULL_THRESHOLD):
            recent = (
                await db.telemetry_timeseries.find(
                    {"meta.container_id": container_id}
                )
                .sort("ts", -1)
                .limit(2)
                .to_list(2)
            )
            if len(recent) >= 2 and all(
                r.get("fused_fill_pct", 0) >= 85 for r in recent
            ):
                await create_system_event(
                    db,
                    container_id,
                    device_id,
                    EventType.FULL_THRESHOLD,
                    EventSeverity.WARNING,
                    f"Container has been full (>= 85%) for 2 consecutive readings.",
                    {"fused_fill_pct": fused, "fill_state": reading.fill.state.value},
                )


async def _check_camera_alerts(
    db: AsyncIOMotorDatabase,
    container_id: str,
    device_id: str,
    reading: TelemetryReading,
) -> None:
    if reading.vision.camera_state == CameraState.CAMERA_FAULT:
        if not await _open_event_exists(db, container_id, EventType.CAMERA_FAULT):
            await create_system_event(
                db,
                container_id,
                device_id,
                EventType.CAMERA_FAULT,
                EventSeverity.WARNING,
                "Camera reported a fault state.",
            )

    # Update latest camera state from telemetry (device events are authoritative,
    # but telemetry keeps latest_state in sync for dashboards).
    if reading.vision.camera_state == CameraState.GARBAGE_DETECTED:
        await db.containers.update_one(
            {"_id": container_id},
            {
                "$set": {
                    "latest_state.camera_state": CameraState.GARBAGE_DETECTED.value,
                    "updated_at": datetime.utcnow(),
                }
            },
        )


async def _check_sensor_fault(
    db: AsyncIOMotorDatabase,
    container_id: str,
    device_id: str,
    reading: TelemetryReading,
) -> None:
    if reading.health.sensor_faults:
        if not await _open_event_exists(db, container_id, EventType.SENSOR_FAULT):
            await create_system_event(
                db,
                container_id,
                device_id,
                EventType.SENSOR_FAULT,
                EventSeverity.WARNING,
                f"Sensor faults reported: {', '.join(reading.health.sensor_faults)}.",
                {"sensor_faults": reading.health.sensor_faults},
            )
