from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from ..models.device import TelemetryBatchRequest
from .alert_rules import run_alert_rules


async def process_telemetry_batch(
    db: AsyncIOMotorDatabase, device: dict, payload: TelemetryBatchRequest
) -> dict:
    message_id = str(payload.message_id)
    now = datetime.utcnow()

    # Dedup check
    try:
        await db.message_dedup.insert_one(
            {
                "device_id": payload.device_id,
                "message_id": message_id,
                "created_at": now,
            }
        )
    except DuplicateKeyError:
        container = await db.containers.find_one({"_id": payload.container_id})
        config_revision = container.get("config_revision", 1) if container else 1
        return {
            "accepted": True,
            "duplicate": True,
            "message_id": message_id,
            "server_time": now.isoformat() + "Z",
            "config_revision": config_revision,
            "commands_available": False,
        }

    # Fetch container for site_id
    container = await db.containers.find_one({"_id": payload.container_id})
    site_id = container.get("site_id") if container else None

    # Insert readings into time-series collection
    ts_docs = []
    for r in payload.readings:
        doc = {
            "ts": r.ts,
            "meta": {
                "device_id": payload.device_id,
                "container_id": payload.container_id,
                "site_id": site_id,
            },
            "temperature_c": r.sensors.temperature_c,
            "humidity_pct": r.sensors.humidity_pct,
            "light_lux": r.sensors.light_lux,
            "ultrasonic_distance_cm": r.sensors.ultrasonic_distance_cm,
            "weight_kg": r.sensors.weight_kg,
            "tamper_open": r.sensors.tamper_open,
            "fill_height_pct": r.fill.height_pct,
            "fill_weight_pct": r.fill.weight_pct,
            "fused_fill_pct": r.fill.fused_pct,
            "fill_state": r.fill.state.value,
            "fill_confidence": r.fill.confidence,
            "camera_state": r.vision.camera_state.value,
            "camera_confidence": r.vision.confidence,
            "model_id": r.vision.model_id,
            "rssi_dbm": r.health.rssi_dbm,
            "uptime_sec": r.health.uptime_sec,
        }
        ts_docs.append(doc)

    if ts_docs:
        await db.telemetry_timeseries.insert_many(ts_docs)

    # Most recent reading by ts (offline buffer may deliver them out of order)
    latest = max(payload.readings, key=lambda r: r.ts)

    # Update container latest_state
    await db.containers.update_one(
        {"_id": payload.container_id},
        {
            "$set": {
                "latest_state.last_seen_at": latest.ts,
                "latest_state.fused_fill_pct": latest.fill.fused_pct,
                "latest_state.fill_state": latest.fill.state.value,
                "latest_state.camera_state": latest.vision.camera_state.value,
                "latest_state.camera_confidence": latest.vision.confidence,
                "latest_state.temperature_c": latest.sensors.temperature_c,
                "latest_state.humidity_pct": latest.sensors.humidity_pct,
                "latest_state.light_lux": latest.sensors.light_lux,
                "latest_state.weight_kg": latest.sensors.weight_kg,
                "latest_state.ultrasonic_distance_cm": latest.sensors.ultrasonic_distance_cm,
                "latest_state.tamper_open": latest.sensors.tamper_open,
                "latest_state.device_status": latest.health.device_status.value,
                "updated_at": now,
            }
        },
    )

    # Update device last_seen_at; reflect health status reported by the device
    await db.devices.update_one(
        {"_id": payload.device_id},
        {"$set": {"last_seen_at": now, "status": latest.health.device_status.value, "updated_at": now}},
    )

    # Run alert rules against the latest reading
    await run_alert_rules(db, payload.container_id, payload.device_id, latest)

    config_revision = container.get("config_revision", 1) if container else 1
    return {
        "accepted": True,
        "duplicate": False,
        "message_id": message_id,
        "server_time": now.isoformat() + "Z",
        "config_revision": config_revision,
        "commands_available": False,
    }
