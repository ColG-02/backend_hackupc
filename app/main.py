import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.config import settings
from .core.database import connect_db, disconnect_db, get_db
from .core.security import hash_password
from .background.offline_monitor import offline_monitor_loop
from .routers import auth, containers, crews, dashboard, device_ingest, devices, events, maintenance, media, realtime, routes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    db = get_db()

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    await _seed_admin(db)

    monitor_task = asyncio.create_task(offline_monitor_loop(db))
    logger.info("Application startup complete.")

    yield

    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    await disconnect_db()
    logger.info("Application shutdown complete.")


async def _seed_admin(db) -> None:
    if await db.users.count_documents({}) == 0:
        await db.users.insert_one(
            {
                "_id": str(uuid4()),
                "email": settings.ADMIN_EMAIL,
                "password_hash": hash_password(settings.ADMIN_PASSWORD),
                "role": "ADMIN",
                "created_at": datetime.utcnow(),
            }
        )
        logger.info("Default admin user created: %s", settings.ADMIN_EMAIL)


app = FastAPI(
    title="Smart Garbage Container API",
    version="1.0.0",
    description="Backend for smart underground garbage container monitoring.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Custom error responses matching spec §17 ──────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(loc) for loc in first.get("loc", []))
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": first.get("msg", "Validation error."),
                "details": {"field": field},
            }
        },
    )


# ── Routers ───────────────────────────────────────────────────────────────────

PREFIX = "/api/v1"

app.include_router(auth.router, prefix=PREFIX)
app.include_router(device_ingest.router, prefix=PREFIX)
app.include_router(containers.router, prefix=PREFIX)
app.include_router(events.router, prefix=PREFIX)
app.include_router(media.router, prefix=PREFIX)
app.include_router(devices.router, prefix=PREFIX)
app.include_router(routes.router, prefix=PREFIX)
app.include_router(crews.router, prefix=PREFIX)
app.include_router(maintenance.router, prefix=PREFIX)
app.include_router(dashboard.router, prefix=PREFIX)
app.include_router(realtime.router, prefix=PREFIX)


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat() + "Z"}
