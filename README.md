# Smart Underground Garbage Container — Backend

REST API backend for a smart underground garbage container monitoring system. Receives sensor telemetry and events from Arduino UNO Q edge devices, stores them in MongoDB Atlas, and exposes a dashboard API for the web frontend.

---

## Table of Contents

- [System Overview](#system-overview)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Authentication](#authentication)
- [User Roles](#user-roles)
- [MongoDB Collections](#mongodb-collections)
- [Data Flows](#data-flows)
- [Background Jobs](#background-jobs)
- [Device Simulator](#device-simulator)
- [API Surface](#api-surface)
- [MVP Implementation Priority](#mvp-implementation-priority)
- [Getting Started](#getting-started)

---

## System Overview

Three actors communicate with this backend:

```
┌─────────────────────────┐
│   Arduino UNO Q device  │  DeviceToken auth
│   (real hardware)       │─────────────────────────────────┐
└─────────────────────────┘                                 │
                                                            ▼
┌─────────────────────────┐                    ┌────────────────────────┐
│  Python device          │  DeviceToken auth  │                        │
│  simulator (N devices)  │───────────────────►│    FastAPI Backend     │◄──── MongoDB Atlas
│  (demo/presentation)    │                    │    /api/v1/...         │
└─────────────────────────┘                    │                        │
                                               └────────────┬───────────┘
┌─────────────────────────┐                                 │
│   Web frontend          │  JWT Bearer auth                │
│   (dashboard / map)     │◄────────────────────────────────┘
└─────────────────────────┘
```

The backend is the single source of truth. It does not know or care whether a device is real hardware or a simulator — both authenticate identically and use the same API endpoints.

---

## Tech Stack

| Layer             | Library                           | Purpose                                                 |
| ----------------- | --------------------------------- | ------------------------------------------------------- |
| Framework         | FastAPI                           | Async REST API, dependency injection, auto OpenAPI docs |
| MongoDB driver    | Motor                             | Async driver — matches FastAPI's async model            |
| Schema validation | Pydantic v2                       | Request/response models, enum validation                |
| User auth         | python-jose + passlib (bcrypt)    | JWT generation and password hashing                     |
| Device auth       | Custom DeviceToken middleware     | Token hash lookup in devices collection                 |
| Background jobs   | asyncio loop via FastAPI lifespan | Offline device detection — no extra dependencies        |
| Image storage     | Local filesystem (MVP)            | `uploads/events/{event_id}/` — swap to S3 later         |
| Route planning    | Not implemented (MVP)             | OR-Tools integration deferred                           |

---

## Project Structure

```
backend/
├── app/
│   ├── main.py                     # FastAPI app, lifespan (startup/shutdown + background jobs)
│   ├── core/
│   │   ├── config.py               # Settings loaded from environment variables
│   │   ├── database.py             # Motor client, collection accessors, index setup
│   │   └── security.py             # DeviceToken verification, JWT encode/decode, role guards
│   ├── models/                     # Pydantic schemas — one file per domain
│   │   ├── common.py               # Shared enums: CameraState, FillState, DeviceStatus, etc.
│   │   ├── device.py               # Bootstrap, heartbeat, config request/response schemas
│   │   ├── container.py            # Container CRUD + latest state schemas
│   │   ├── event.py                # Event create/update/list schemas
│   │   ├── media.py                # Media metadata schema
│   │   ├── route.py                # Route plan schemas
│   │   ├── maintenance.py          # Maintenance ticket schemas
│   │   └── user.py                 # User + auth schemas
│   ├── routers/                    # HTTP layer — one file per spec section
│   │   ├── auth.py                 # POST /auth/login → JWT
│   │   ├── device_ingest.py        # §8: bootstrap, telemetry, events, heartbeat, config
│   │   ├── containers.py           # §9: container CRUD, latest state, telemetry history
│   │   ├── events.py               # §10: list, get, acknowledge, resolve, ignore
│   │   ├── media.py                # §11: metadata, file serve
│   │   ├── devices.py              # §12: list, get, assign, config update
│   │   ├── routes.py               # §13: route plan CRUD (algorithm deferred)
│   │   └── maintenance.py          # §14: maintenance ticket CRUD
│   ├── services/                   # Business logic, decoupled from HTTP layer
│   │   ├── telemetry.py            # Dedup, timeseries insert, latest_state update
│   │   ├── event_service.py        # Event lifecycle, GARBAGE_CLEARED resolution
│   │   ├── alert_rules.py          # §16 fill/camera/offline/sensor-fault alert rules
│   │   ├── route_service.py        # Candidate selection + priority scoring (no optimizer yet)
│   │   └── media_service.py        # Save/read image files (local disk)
│   └── background/
│       └── offline_monitor.py      # Periodic check: devices with no heartbeat > 10 min
├── simulator/
│   ├── main.py                     # Entry point: spawns N simulated devices
│   ├── config.py                   # BACKEND_URL, NUM_DEVICES, SPEED_FACTOR, scenario
│   ├── device_sim.py               # Single device state machine + HTTP client
│   ├── scenarios.py                # Preset demo scenarios (fill cycle, garbage detection, etc.)
│   └── state_store.json            # Persisted device tokens — skips re-bootstrap on restart
├── uploads/                        # Local image storage: events/{event_id}/{media_id}.jpg
├── docs/
│   └── smart_garbage_container_api_spec.md
├── .env.example
├── requirements.txt
└── README.md
```

---

## Authentication

Two completely separate auth flows exist in parallel. No endpoint accepts both.

### Device authentication (Arduino + simulator)

All `/api/v1/device/*` endpoints require:

```http
Authorization: DeviceToken <token>
X-Device-Id: cont-000123
```

On receiving a request, the backend:

1. Reads `device_id` from the `X-Device-Id` header.
2. Looks up the device document in MongoDB.
3. Compares the provided token against the stored bcrypt hash (`device_token_hash`).
4. Checks that the device `status` is not `DISABLED`.
5. Injects the device document into the route handler via FastAPI dependency.

The token is issued once during `POST /device/bootstrap` and never rotated automatically in MVP.

### User authentication (web dashboard)

All `/api/v1/containers`, `/api/v1/events`, `/api/v1/devices`, etc. require:

```http
Authorization: Bearer <jwt>
```

Flow:

```
POST /api/v1/auth/login  { email, password }
  └─► verify bcrypt hash against users collection
  └─► issue JWT containing { user_id, email, role, exp }
  └─► return { access_token, token_type: "bearer" }

Subsequent requests:
  └─► FastAPI dependency decodes JWT
  └─► injects current user into route handler
  └─► role guard rejects if insufficient role
```

JWT expiry is configurable via `ACCESS_TOKEN_EXPIRE_MINUTES` env var. No refresh token in MVP.

---

## User Roles

| Role         | Containers |  Events  | Devices | Routes | Maintenance | Admin |
| ------------ | :--------: | :------: | :-----: | :----: | :---------: | :---: |
| `ADMIN`      |    R/W     |   R/W    |   R/W   |  R/W   |     R/W     |  Yes  |
| `DISPATCHER` |    Read    | Read/Ack |  Read   |  R/W   |    Read     |  No   |
| `VIEWER`     |    Read    |   Read   |  Read   |  Read  |    Read     |  No   |

Role is stored in the `users` collection and embedded in the JWT payload.  
`ADMIN` is the only role that can create/update containers, update device config, and create users.

---

## MongoDB Collections

```
users               — web dashboard accounts
devices             — registered edge devices (real + simulated)
containers          — physical garbage container registry
telemetry_timeseries — time-series sensor readings (MongoDB native time-series collection)
events              — event lifecycle (GARBAGE_DETECTED, FULL_THRESHOLD, etc.)
media               — image upload metadata (file lives on disk)
maintenance_tickets — fault/maintenance tracking
route_plans         — collection route plans with stops
audit_log           — (post-MVP) user action log
```

### Relationships

```
devices._id  ◄────────────────  containers.device_id
containers._id  ◄─────────────  telemetry_timeseries.meta.container_id
containers._id  ◄─────────────  events.container_id
events._id  ◄─────────────────  media.event_id
containers._id  ◄─────────────  maintenance_tickets.container_id
containers._id  ◄─────────────  route_plans[].routes[].stops[].container_id
```

### `telemetry_timeseries` — time-series collection

Created with MongoDB native time-series support for efficient time-range queries:

```js
db.createCollection("telemetry_timeseries", {
    timeseries: {
        timeField: "ts",
        metaField: "meta", // device_id, container_id, site_id
        granularity: "minutes",
    },
});
```

The dashboard reads **`containers.latest_state`** (a denormalized snapshot) for real-time display. The `telemetry_timeseries` collection is only queried for historical charts and trend data.

---

## Data Flows

### Telemetry ingest

```
Device  POST /api/v1/device/telemetry
         │
         ├─ 1. Verify DeviceToken (security.py)
         ├─ 2. Check message_id dedup (device_id + message_id unique index on events)
         ├─ 3. Insert each reading into telemetry_timeseries
         ├─ 4. $set containers.latest_state with most recent reading
         ├─ 5. Run alert_rules:
         │       ├─ fused_fill_pct >= 95  →  create CRITICAL_FULL event
         │       ├─ fused_fill_pct >= 85 (2 consecutive)  →  create FULL_THRESHOLD event
         │       └─ camera_state = GARBAGE_DETECTED  →  update latest_state
         └─ 6. Return { accepted, config_revision, commands_available }
```

### Event lifecycle

```
Device  POST /api/v1/device/events  { type: GARBAGE_DETECTED }
         │
         ├─ Create event doc  status=OPEN
         ├─ Update containers.latest_state.camera_state = GARBAGE_DETECTED
         └─ If evidence.image_available: respond with upload_image=true + media_upload_url

Device  POST /api/v1/device/events  { type: GARBAGE_CLEARED }
         │
         ├─ Find open GARBAGE_DETECTED event for same container_id
         ├─ Set status=RESOLVED, ended_at=now
         └─ Update containers.latest_state.camera_state = EVERYTHING_OK

Dashboard  POST /api/v1/events/{id}/acknowledge
           POST /api/v1/events/{id}/resolve
           POST /api/v1/events/{id}/ignore
         └─ Update event status + recorded_by fields
```

### Route stop completion

```
Dashboard  PATCH /api/v1/routes/plans/{plan_id}/stops/{stop_id}  { status: COMPLETED }
         │
         ├─ Mark stop as COMPLETED
         ├─ Update containers.last_collected_at
         ├─ Resolve open GARBAGE_DETECTED / FULL_THRESHOLD / CRITICAL_FULL events
         └─ Create COLLECTION_CONFIRMED event
```

### Device bootstrap

```
Device  POST /api/v1/device/bootstrap  { factory_device_id, claim_code, firmware, capabilities }
         │
         ├─ Validate claim_code (pre-provisioned in DB by admin)
         ├─ Check factory_device_id not already claimed (409 if so)
         ├─ Create device doc with generated device_id
         ├─ Generate device token, store bcrypt hash
         └─ Return { device_id, container_id, device_token, config }
```

---

## Background Jobs

### Offline device monitor

Runs as an `asyncio` loop started in FastAPI's `lifespan` context (no external scheduler needed).

```
Every 5 minutes:
  │
  ├─ Query devices where last_seen_at < (now - 10 minutes) AND status != OFFLINE
  │     └─ For each: set status=OFFLINE, create DEVICE_OFFLINE event
  │
  └─ Query devices where status=OFFLINE AND last_seen_at >= (now - 10 minutes)
        └─ For each: set status=ONLINE, create DEVICE_ONLINE event
```

---

## Device Simulator

For demo and presentation purposes, a standalone Python simulator runs alongside the backend. It communicates exclusively through the real API — the backend cannot distinguish a simulated device from the real Arduino.

### How it works

```
simulator/main.py --devices 8 --speed 60 --scenario full_cycle
         │
         ├─ Load state_store.json (saved tokens from previous run)
         │
         ├─ For any device not yet bootstrapped:
         │     POST /api/v1/device/bootstrap  →  receive device_id + token
         │     Save to state_store.json
         │
         └─ Spawn N asyncio coroutines (one per device)
               Each coroutine runs independently:
               ├─ Telemetry loop  (interval from config, accelerated by SPEED_FACTOR)
               ├─ Heartbeat loop
               └─ State machine:  EMPTY → NORMAL → NEAR_FULL → FULL → CRITICAL
                                           ↑                              │
                                           └────── fill resets on collection ──┘
```

`SPEED_FACTOR=60` means 1 real second = 1 simulated minute, so a full fill cycle completes in ~2 minutes of real time.

### State machine (per device)

```
         fill_rate per tick
EMPTY ──────────────────► NORMAL ──► NEAR_FULL ──► FULL ──► CRITICAL
                                         │
                   random probability    │
                   ┌────────────────────►│
                   │                     ▼
                   │           GARBAGE_DETECTED event sent
                   │           (+ optional image upload)
                   │                     │
                   │           after random delay
                   │                     ▼
                   └──────── GARBAGE_CLEARED event sent
```

### Demo scenarios

| Scenario              | Description                                                               |
| --------------------- | ------------------------------------------------------------------------- |
| `normal_operation`    | All devices run with gradual fill increase                                |
| `garbage_detection`   | One device fires GARBAGE_DETECTED, uploads sample image, then clears      |
| `device_goes_offline` | One device stops sending heartbeats for 12 min, triggering DEVICE_OFFLINE |
| `fill_critical`       | One device fills to CRITICAL rapidly                                      |
| `full_cycle`          | Devices fill → route planned → stops completed → fill resets              |

### Real device + simulator on same dashboard

```
Dashboard map:
  ● bin-bg-001  (real Arduino UNO Q)   — NEAR_FULL, camera OK
  ● bin-sim-001 (Python simulator)     — FULL, GARBAGE_DETECTED
  ● bin-sim-002 (Python simulator)     — NORMAL
  ● bin-sim-003 (Python simulator)     — CRITICAL
  ...
```

Both appear identically in the dashboard. Mix of real + simulated data makes the demo convincing without requiring multiple hardware units.

---

## API Surface

### Device-facing endpoints (DeviceToken auth)

| Method | Path                                     | Description                    |
| ------ | ---------------------------------------- | ------------------------------ |
| POST   | `/api/v1/device/bootstrap`               | First-time device registration |
| POST   | `/api/v1/device/telemetry`               | Upload sensor reading batch    |
| POST   | `/api/v1/device/events`                  | Report a device event          |
| POST   | `/api/v1/device/events/{event_id}/media` | Upload event image             |
| POST   | `/api/v1/device/heartbeat`               | Periodic liveness signal       |
| GET    | `/api/v1/device/config`                  | Poll for config changes        |
| POST   | `/api/v1/device/config/ack`              | Confirm config applied         |

### Dashboard/admin endpoints (JWT Bearer auth)

| Method | Path                                        | Roles             | Description                       |
| ------ | ------------------------------------------- | ----------------- | --------------------------------- |
| POST   | `/api/v1/auth/login`                        | —                 | Get JWT token                     |
| GET    | `/api/v1/containers`                        | All               | List containers with latest state |
| POST   | `/api/v1/containers`                        | Admin             | Create container                  |
| GET    | `/api/v1/containers/{id}`                   | All               | Container details                 |
| PATCH  | `/api/v1/containers/{id}`                   | Admin             | Update container                  |
| GET    | `/api/v1/containers/{id}/latest`            | All               | Live sensor snapshot              |
| GET    | `/api/v1/containers/{id}/telemetry`         | All               | Historical telemetry              |
| GET    | `/api/v1/events`                            | All               | List events (filterable)          |
| GET    | `/api/v1/events/{id}`                       | All               | Event details                     |
| POST   | `/api/v1/events/{id}/acknowledge`           | Admin, Dispatcher | Acknowledge event                 |
| POST   | `/api/v1/events/{id}/resolve`               | Admin, Dispatcher | Resolve event                     |
| POST   | `/api/v1/events/{id}/ignore`                | Admin, Dispatcher | Ignore event                      |
| GET    | `/api/v1/media/{id}`                        | All               | Media metadata                    |
| GET    | `/api/v1/media/{id}/file`                   | All               | Download image                    |
| GET    | `/api/v1/devices`                           | All               | List devices                      |
| GET    | `/api/v1/devices/{id}`                      | All               | Device details                    |
| POST   | `/api/v1/devices/{id}/assign`               | Admin             | Assign device to container        |
| PATCH  | `/api/v1/devices/{id}/config`               | Admin             | Push new config to device         |
| POST   | `/api/v1/routes/plan`                       | Admin, Dispatcher | Generate route plan               |
| GET    | `/api/v1/routes/plans`                      | All               | List route plans                  |
| GET    | `/api/v1/routes/plans/{id}`                 | All               | Route plan details                |
| POST   | `/api/v1/routes/plans/{id}/dispatch`        | Admin, Dispatcher | Dispatch plan to drivers          |
| PATCH  | `/api/v1/routes/plans/{id}/stops/{stop_id}` | Admin, Dispatcher | Mark stop completed               |
| GET    | `/api/v1/maintenance/tickets`               | All               | List maintenance tickets          |
| POST   | `/api/v1/maintenance/tickets`               | Admin             | Create ticket                     |
| PATCH  | `/api/v1/maintenance/tickets/{id}`          | Admin             | Update ticket                     |

---

## MVP Implementation Priority

### Phase 1 — Device pipeline (everything the Arduino needs)

- [ ] `core/config.py` — env-var settings
- [ ] `core/database.py` — Motor client + collection setup + indexes
- [ ] `core/security.py` — DeviceToken verification + JWT helpers
- [ ] `models/common.py` — all enums
- [ ] `routers/device_ingest.py` — bootstrap, telemetry, events, heartbeat, config
- [ ] `services/telemetry.py` — dedup + timeseries insert + latest_state update
- [ ] `services/event_service.py` — event lifecycle
- [ ] `services/alert_rules.py` — fill/camera alert generation

### Phase 2 — Dashboard API

- [ ] `routers/auth.py` — login endpoint
- [ ] `routers/containers.py` — list, get, create, latest, telemetry history
- [ ] `routers/events.py` — list, get, ack, resolve, ignore
- [ ] `routers/devices.py` — list, get, assign, config update
- [ ] `routers/media.py` — metadata + file serve

### Phase 3 — Operations

- [ ] `background/offline_monitor.py` — asyncio loop
- [ ] `routers/routes.py` — route plan CRUD (no optimizer, greedy sort by priority score)
- [ ] `routers/maintenance.py` — ticket CRUD

### Phase 4 — Simulator

- [ ] `simulator/device_sim.py` — single device state machine
- [ ] `simulator/main.py` — spawn N devices
- [ ] `simulator/scenarios.py` — demo scenarios

### Deferred (post-MVP)

- OR-Tools route optimization
- Image storage on S3 / object storage
- HMAC device authentication
- Audit log
- Refresh tokens / token rotation
- Predictive fill forecasting

---

## Getting Started

### Prerequisites

- Python 3.11+
- MongoDB Atlas cluster (or local `mongod` 6.0+)

### Environment variables

Copy `.env.example` to `.env` and fill in:

```env
MONGODB_URI=mongodb+srv://<user>:<pass>@cluster.mongodb.net/smart_waste
DATABASE_NAME=smart_waste

JWT_SECRET_KEY=change-this-to-a-random-secret
ACCESS_TOKEN_EXPIRE_MINUTES=480

UPLOAD_DIR=uploads
MAX_IMAGE_SIZE_MB=2
```

### Run the backend

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

OpenAPI docs available at `http://localhost:8080/docs`.

### Run the simulator

```bash
# Normal demo speed, 8 devices, mixed scenario
python simulator/main.py --devices 8 --speed 60 --scenario normal_operation

# Full cycle demo (fill → route → collect → reset)
python simulator/main.py --devices 5 --speed 120 --scenario full_cycle
```

### Minimal test flow (from spec §20)

```bash
# 1. Create a container
curl -X POST http://localhost:8080/api/v1/containers \
  -H "Authorization: Bearer <admin-jwt>" \
  -d '{"container_id":"bin-bg-001","name":"Main Square","location":{"type":"Point","coordinates":[20.4573,44.8176]}}'

# 2. Bootstrap a device
curl -X POST http://localhost:8080/api/v1/device/bootstrap \
  -d '{"schema_version":"1.0","factory_device_id":"unoq-abc123","claim_code":"TEAM-DEMO-123456",...}'

# 3–12. See full curl examples in docs/smart_garbage_container_api_spec.md §21
```
