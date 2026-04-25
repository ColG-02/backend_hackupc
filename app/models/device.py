from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .common import CameraState, DeviceStatus, FillState


# ── Shared sub-models ────────────────────────────────────────────────────────

class FirmwareInfo(BaseModel):
    mcu_version: str
    linux_app_version: str
    model_id: str


class DeviceCapabilities(BaseModel):
    sensors: list[str]
    camera: bool
    offline_buffer: bool


class DeviceThresholds(BaseModel):
    near_full_pct: float = 70
    full_pct: float = 85
    critical_pct: float = 95
    garbage_confidence: float = 0.65
    garbage_frames_required: int = 3
    garbage_window_frames: int = 5
    clear_frames_required: int = 5


class DeviceCalibration(BaseModel):
    empty_distance_cm: float = 130
    full_distance_cm: float = 20
    empty_weight_kg: float = 40
    max_payload_kg: float = 400


class CameraConfig(BaseModel):
    resolution_width: int = 1280
    resolution_height: int = 720
    jpeg_quality: int = 80
    flip_horizontal: bool = False
    flip_vertical: bool = False


class DeviceConfig(BaseModel):
    telemetry_interval_sec: int = 60
    heartbeat_interval_sec: int = 60
    camera_inference_interval_ms: int = 2000
    upload_event_images: bool = True
    thresholds: DeviceThresholds = DeviceThresholds()
    calibration: DeviceCalibration = DeviceCalibration()
    camera: CameraConfig | None = None


# ── Bootstrap ────────────────────────────────────────────────────────────────

class BootstrapRequest(BaseModel):
    schema_version: str
    factory_device_id: str
    claim_code: str
    firmware: FirmwareInfo
    capabilities: DeviceCapabilities


class BootstrapResponse(BaseModel):
    accepted: bool = True
    device_id: str
    container_id: str
    device_token: str
    server_time: str
    config_revision: int
    config: DeviceConfig


# ── Telemetry ────────────────────────────────────────────────────────────────

class SensorData(BaseModel):
    temperature_c: float | None = None
    humidity_pct: float | None = None
    light_lux: float | None = None
    ultrasonic_distance_cm: float | None = None
    weight_kg: float | None = None
    tamper_open: bool | None = None


class FillData(BaseModel):
    height_pct: float
    weight_pct: float
    fused_pct: float
    state: FillState
    confidence: float


class VisionData(BaseModel):
    model_id: str
    camera_state: CameraState
    confidence: float
    last_inference_at: datetime


class HealthData(BaseModel):
    device_status: DeviceStatus
    rssi_dbm: int | None = None
    uptime_sec: int | None = None
    cpu_temp_c: float | None = None
    free_disk_mb: int | None = None
    offline_queue_count: int = 0
    sensor_faults: list[str] = []
    camera_fault: bool = False


class TelemetryReading(BaseModel):
    ts: datetime
    sensors: SensorData
    fill: FillData
    vision: VisionData
    health: HealthData


class TelemetryBatchRequest(BaseModel):
    schema_version: str
    message_id: UUID
    device_id: str
    container_id: str
    sent_at: datetime
    seq: int
    readings: list[TelemetryReading]


class TelemetryResponse(BaseModel):
    accepted: bool = True
    duplicate: bool = False
    message_id: str
    server_time: str
    config_revision: int
    commands_available: bool = False


# ── Events ───────────────────────────────────────────────────────────────────

class EventState(BaseModel):
    camera_state: CameraState | None = None
    fused_fill_pct: float | None = None
    fill_state: FillState | None = None


class EventEvidence(BaseModel):
    image_available: bool = False
    local_image_id: str | None = None


class DeviceEventPayload(BaseModel):
    type: str
    severity: str
    started_at: datetime
    ended_at: datetime | None = None
    confidence: float | None = None
    summary: str
    state: EventState
    evidence: EventEvidence | None = None


class DeviceEventRequest(BaseModel):
    schema_version: str
    message_id: UUID
    device_id: str
    container_id: str
    sent_at: datetime
    seq: int
    event: DeviceEventPayload


class DeviceEventResponse(BaseModel):
    accepted: bool = True
    event_id: str
    upload_image: bool = False
    media_upload_url: str | None = None
    server_time: str


# ── Heartbeat ────────────────────────────────────────────────────────────────

class HeartbeatHealth(BaseModel):
    uptime_sec: int | None = None
    rssi_dbm: int | None = None
    cpu_temp_c: float | None = None
    free_disk_mb: int | None = None
    offline_queue_count: int = 0
    last_sensor_sample_at: datetime | None = None
    last_camera_frame_at: datetime | None = None
    last_successful_upload_at: datetime | None = None


class HeartbeatRequest(BaseModel):
    schema_version: str
    message_id: UUID
    device_id: str
    container_id: str
    sent_at: datetime
    seq: int
    status: DeviceStatus
    firmware: FirmwareInfo
    health: HeartbeatHealth


class HeartbeatResponse(BaseModel):
    accepted: bool = True
    server_time: str
    config_revision: int
    commands_available: bool = False


# ── Config pull / ack ────────────────────────────────────────────────────────

class ConfigResponse(BaseModel):
    changed: bool
    config_revision: int
    server_time: str
    config: DeviceConfig | None = None


class ConfigAckRequest(BaseModel):
    schema_version: str
    message_id: UUID
    device_id: str
    container_id: str
    sent_at: datetime
    seq: int
    config_revision: int
    applied_at: datetime
    success: bool
    error: str | None = None


class ConfigAckResponse(BaseModel):
    accepted: bool = True
    config_revision: int


# ── Device management (admin dashboard) ──────────────────────────────────────

class DeviceOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    device_id: str
    container_id: str | None = None
    status: DeviceStatus
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    firmware: FirmwareInfo | None = None
    health: dict | None = None


class DeviceListResponse(BaseModel):
    items: list[DeviceOut]
    total: int


class AssignDeviceRequest(BaseModel):
    container_id: str


class AssignDeviceResponse(BaseModel):
    assigned: bool = True
    device_id: str
    container_id: str


class UpdateDeviceConfigRequest(BaseModel):
    telemetry_interval_sec: int | None = None
    heartbeat_interval_sec: int | None = None
    camera_inference_interval_ms: int | None = None
    upload_event_images: bool | None = None
    thresholds: DeviceThresholds | None = None
    calibration: DeviceCalibration | None = None


class UpdateDeviceConfigResponse(BaseModel):
    updated: bool = True
    device_id: str
    config_revision: int


# ── Claim codes (admin utility) ───────────────────────────────────────────────

class CreateClaimCodeRequest(BaseModel):
    container_id: str
    code: str | None = None


class ClaimCodeResponse(BaseModel):
    code: str
    container_id: str
