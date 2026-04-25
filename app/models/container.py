from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .common import CameraState, DeviceStatus, FillState


class GeoPoint(BaseModel):
    type: str = "Point"
    coordinates: list[float]


class ContainerCapacity(BaseModel):
    volume_l: float | None = None
    max_payload_kg: float | None = None


class LatestState(BaseModel):
    last_seen_at: datetime | None = None
    fused_fill_pct: float | None = None
    fill_state: FillState | None = None
    camera_state: CameraState | None = None
    camera_confidence: float | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    light_lux: float | None = None
    weight_kg: float | None = None
    ultrasonic_distance_cm: float | None = None
    tamper_open: bool | None = None
    device_status: DeviceStatus | None = None


# ── Create ────────────────────────────────────────────────────────────────────

class CreateContainerRequest(BaseModel):
    container_id: str
    name: str
    site_id: str | None = None
    location: GeoPoint | None = None
    address: str | None = None
    container_type: str | None = None
    capacity: ContainerCapacity | None = None


class CreateContainerResponse(BaseModel):
    container_id: str
    created: bool = True


# ── Update ────────────────────────────────────────────────────────────────────

class UpdateContainerRequest(BaseModel):
    name: str | None = None
    site_id: str | None = None
    address: str | None = None
    capacity: ContainerCapacity | None = None


class UpdateContainerResponse(BaseModel):
    updated: bool = True
    container_id: str


# ── Read ──────────────────────────────────────────────────────────────────────

class ContainerSummary(BaseModel):
    container_id: str
    name: str
    site_id: str | None = None
    location: GeoPoint | None = None
    status: str | None = None
    assigned_device_id: str | None = None
    latest_state: LatestState | None = None


class ContainerDetail(BaseModel):
    container_id: str
    name: str
    site_id: str | None = None
    location: GeoPoint | None = None
    address: str | None = None
    status: str | None = None
    container_type: str | None = None
    capacity: ContainerCapacity | None = None
    assigned_device_id: str | None = None
    latest_state: LatestState | None = None
    config_revision: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ContainerListResponse(BaseModel):
    items: list[ContainerSummary]
    total: int
    limit: int
    offset: int


# ── Latest state ──────────────────────────────────────────────────────────────

class FillSnapshot(BaseModel):
    height_pct: float | None = None
    weight_pct: float | None = None
    fused_pct: float | None = None
    state: FillState | None = None
    confidence: float | None = None


class VisionSnapshot(BaseModel):
    camera_state: CameraState | None = None
    confidence: float | None = None
    model_id: str | None = None
    last_inference_at: datetime | None = None


class SensorSnapshot(BaseModel):
    temperature_c: float | None = None
    humidity_pct: float | None = None
    light_lux: float | None = None
    ultrasonic_distance_cm: float | None = None
    weight_kg: float | None = None
    tamper_open: bool | None = None


class HealthSnapshot(BaseModel):
    device_status: DeviceStatus | None = None
    rssi_dbm: int | None = None
    uptime_sec: int | None = None
    offline_queue_count: int | None = None


class ContainerLatestResponse(BaseModel):
    container_id: str
    last_seen_at: datetime | None = None
    fill: FillSnapshot | None = None
    vision: VisionSnapshot | None = None
    sensors: SensorSnapshot | None = None
    health: HealthSnapshot | None = None


# ── Telemetry history ─────────────────────────────────────────────────────────

class TelemetryHistoryItem(BaseModel):
    ts: datetime
    temperature_c: float | None = None
    humidity_pct: float | None = None
    fused_fill_pct: float | None = None
    weight_kg: float | None = None
    camera_state: CameraState | None = None


class TelemetryHistoryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    container_id: str
    from_: datetime = Field(serialization_alias="from")
    to: datetime
    interval: str
    items: list[TelemetryHistoryItem]
