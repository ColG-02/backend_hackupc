from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from .config import settings

_client: AsyncIOMotorClient | None = None


def get_db() -> AsyncIOMotorDatabase:
    assert _client is not None, "Database client not initialised"
    return _client[settings.DATABASE_NAME]


async def connect_db() -> None:
    global _client
    _client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = get_db()
    await _setup_collections(db)
    await _setup_indexes(db)


async def disconnect_db() -> None:
    global _client
    if _client:
        _client.close()
        _client = None


async def _setup_collections(db: AsyncIOMotorDatabase) -> None:
    existing = await db.list_collection_names()
    if "telemetry_timeseries" not in existing:
        await db.create_collection(
            "telemetry_timeseries",
            timeseries={
                "timeField": "ts",
                "metaField": "meta",
                "granularity": "minutes",
            },
        )


async def _setup_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.telemetry_timeseries.create_index([("meta.container_id", 1), ("ts", -1)])
    await db.telemetry_timeseries.create_index([("meta.device_id", 1), ("ts", -1)])

    await db.containers.create_index([("location", "2dsphere")])
    await db.containers.create_index([("device_id", 1)], unique=True, sparse=True)
    await db.containers.create_index([("site_id", 1)])
    await db.containers.create_index([("latest_state.fill_state", 1)])
    await db.containers.create_index([("latest_state.camera_state", 1)])

    await db.devices.create_index([("factory_device_id", 1)], unique=True)
    await db.devices.create_index([("container_id", 1)])
    await db.devices.create_index([("status", 1)])
    await db.devices.create_index([("last_seen_at", 1)])

    await db.events.create_index(
        [("device_id", 1), ("message_id", 1)], unique=True, sparse=True
    )
    await db.events.create_index([("container_id", 1), ("started_at", -1)])
    await db.events.create_index([("status", 1), ("severity", 1)])
    await db.events.create_index([("type", 1), ("started_at", -1)])

    # TTL of 7 days for telemetry dedup records
    await db.message_dedup.create_index(
        [("device_id", 1), ("message_id", 1)], unique=True
    )
    await db.message_dedup.create_index(
        [("created_at", 1)], expireAfterSeconds=604800
    )

    await db.users.create_index([("email", 1)], unique=True)

    await db.claim_codes.create_index([("code", 1)], unique=True)
    await db.claim_codes.create_index([("container_id", 1)])

    await db.maintenance_tickets.create_index([("container_id", 1)])
    await db.maintenance_tickets.create_index([("status", 1)])
