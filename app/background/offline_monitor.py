import asyncio
import logging
from datetime import datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..models.common import DeviceStatus, EventSeverity, EventType
from ..services.event_service import create_system_event

logger = logging.getLogger(__name__)

OFFLINE_THRESHOLD_MINUTES = 10
CHECK_INTERVAL_SECONDS = 300  # 5 minutes


async def offline_monitor_loop(db: AsyncIOMotorDatabase) -> None:
    logger.info("Offline monitor started (check every %ds).", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            await _check_offline_devices(db)
        except asyncio.CancelledError:
            logger.info("Offline monitor stopped.")
            break
        except Exception:
            logger.exception("Error in offline monitor — continuing.")


async def _check_offline_devices(db: AsyncIOMotorDatabase) -> None:
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=OFFLINE_THRESHOLD_MINUTES)

    # Devices that have gone silent and are not yet marked OFFLINE
    gone_silent = await db.devices.find(
        {
            "status": {"$ne": DeviceStatus.OFFLINE.value},
            "$or": [
                {"last_seen_at": {"$lt": cutoff}},
                {"last_seen_at": {"$exists": False}},
            ],
        }
    ).to_list(None)

    for device in gone_silent:
        device_id = device["_id"]
        container_id = device.get("container_id", "")
        await db.devices.update_one(
            {"_id": device_id},
            {
                "$set": {
                    "status": DeviceStatus.OFFLINE.value,
                    "updated_at": now,
                }
            },
        )
        if container_id:
            await db.containers.update_one(
                {"_id": container_id},
                {
                    "$set": {
                        "latest_state.device_status": DeviceStatus.OFFLINE.value,
                        "updated_at": now,
                    }
                },
            )
            await create_system_event(
                db,
                container_id,
                device_id,
                EventType.DEVICE_OFFLINE,
                EventSeverity.WARNING,
                f"Device {device_id} has not sent a heartbeat for over {OFFLINE_THRESHOLD_MINUTES} minutes.",
            )
        logger.warning("Device %s marked OFFLINE.", device_id)

    # Devices that have reconnected (OFFLINE but have a recent last_seen_at)
    reconnected = await db.devices.find(
        {
            "status": DeviceStatus.OFFLINE.value,
            "last_seen_at": {"$gte": cutoff},
        }
    ).to_list(None)

    for device in reconnected:
        device_id = device["_id"]
        container_id = device.get("container_id", "")
        await db.devices.update_one(
            {"_id": device_id},
            {
                "$set": {
                    "status": DeviceStatus.ONLINE.value,
                    "updated_at": now,
                }
            },
        )
        if container_id:
            await db.containers.update_one(
                {"_id": container_id},
                {
                    "$set": {
                        "latest_state.device_status": DeviceStatus.ONLINE.value,
                        "updated_at": now,
                    }
                },
            )
            await create_system_event(
                db,
                container_id,
                device_id,
                EventType.DEVICE_ONLINE,
                EventSeverity.INFO,
                f"Device {device_id} reconnected after being offline.",
            )
        logger.info("Device %s marked ONLINE (reconnected).", device_id)
