"""
Single simulated device.

Each instance runs two concurrent asyncio loops:
  - telemetry_loop: sends sensor readings at the configured interval
  - heartbeat_loop: sends heartbeats at the configured interval

State machine (fill level):
  EMPTY → NORMAL → NEAR_FULL → FULL → CRITICAL
  After reaching CRITICAL the device resets fill (simulating garbage collection)
  without needing the route API to be called.

Camera events fire randomly when fill > 20% and self-resolve after a short delay.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _fill_state(pct: float) -> str:
    if pct < 20:
        return "EMPTY"
    if pct < 70:
        return "NORMAL"
    if pct < 85:
        return "NEAR_FULL"
    if pct < 95:
        return "FULL"
    return "CRITICAL"


class DeviceSimulator:
    def __init__(
        self,
        *,
        sim_index: int,
        device_id: str,
        container_id: str,
        device_token: str,
        config: dict,
        backend_url: str,
        speed_factor: float,
        location: tuple[float, float] | None = None,
        paused: bool = False,
    ):
        self.sim_index = sim_index
        self.device_id = device_id
        self.container_id = container_id
        self.token = device_token
        self.config = config
        self.backend_url = backend_url.rstrip("/")
        self.speed_factor = speed_factor
        self.location = location
        self.paused = paused  # if True, stop sending (simulates offline)

        # Sensor state
        self.fill_pct: float = random.uniform(5.0, 30.0)
        self.fill_rate: float = random.uniform(0.8, 2.5)  # % per simulated minute
        self.temperature: float = random.uniform(15.0, 28.0)
        self.humidity: float = random.uniform(35.0, 75.0)
        self.light_lux: float = random.uniform(50.0, 400.0)

        # Derived from fill
        self.weight_kg: float = 40.0 + (self.fill_pct / 100.0) * 360.0

        self.rssi: int = random.randint(-80, -45)
        self.uptime_sec: int = 0
        self.seq: int = 0

        # Camera state
        self.camera_state: str = "EVERYTHING_OK"
        self.garbage_event_id: str | None = None
        self.garbage_clear_ticks: int = 0

        # Track config revision
        self.config_revision: int = config.get("config_revision", 1)

    def _headers(self) -> dict:
        return {
            "Authorization": f"DeviceToken {self.token}",
            "X-Device-Id": self.device_id,
            "Content-Type": "application/json",
        }

    def _real_interval(self, simulated_seconds: int) -> float:
        return simulated_seconds / self.speed_factor

    def _advance_state(self) -> None:
        telemetry_interval = self.config.get("telemetry_interval_sec", 60)

        self.fill_pct = min(100.0, self.fill_pct + self.fill_rate * (telemetry_interval / 60.0))
        self.weight_kg = 40.0 + (self.fill_pct / 100.0) * 360.0
        self.temperature += random.uniform(-0.3, 0.3)
        self.humidity = max(10.0, min(95.0, self.humidity + random.uniform(-0.5, 0.5)))
        self.light_lux = max(0.0, self.light_lux + random.uniform(-10.0, 10.0))
        self.rssi = max(-100, min(-30, self.rssi + random.randint(-2, 2)))
        self.uptime_sec += telemetry_interval
        self.seq += 1

        # Camera: randomly detect garbage when fill > 20%
        if self.camera_state == "EVERYTHING_OK" and self.fill_pct > 20:
            if random.random() < 0.04:
                self.camera_state = "GARBAGE_DETECTED"
                self.garbage_clear_ticks = random.randint(3, 8)

        elif self.camera_state == "GARBAGE_DETECTED":
            self.garbage_clear_ticks -= 1
            if self.garbage_clear_ticks <= 0:
                self.camera_state = "EVERYTHING_OK"

        # Auto-reset fill at CRITICAL (simulates collection without route API)
        if self.fill_pct >= 98:
            logger.info(
                "[sim-%02d] Fill reached %.0f%% — simulating collection, resetting fill.",
                self.sim_index,
                self.fill_pct,
            )
            self.fill_pct = random.uniform(5.0, 15.0)
            self.weight_kg = 40.0 + (self.fill_pct / 100.0) * 360.0
            self.camera_state = "EVERYTHING_OK"
            self.garbage_event_id = None

    def _build_telemetry_payload(self) -> dict:
        return {
            "schema_version": "1.0",
            "message_id": str(uuid4()),
            "device_id": self.device_id,
            "container_id": self.container_id,
            "sent_at": _utcnow(),
            "seq": self.seq,
            "readings": [
                {
                    "ts": _utcnow(),
                    "sensors": {
                        "temperature_c": round(self.temperature, 1),
                        "humidity_pct": round(self.humidity, 1),
                        "light_lux": round(self.light_lux, 1),
                        "ultrasonic_distance_cm": round(
                            130.0 - (self.fill_pct / 100.0) * 110.0, 1
                        ),
                        "weight_kg": round(self.weight_kg, 1),
                        "tamper_open": False,
                    },
                    "fill": {
                        "height_pct": round(self.fill_pct, 1),
                        "weight_pct": round(max(0, self.fill_pct - random.uniform(-5, 5)), 1),
                        "fused_pct": round(self.fill_pct, 1),
                        "state": _fill_state(self.fill_pct),
                        "confidence": round(random.uniform(0.78, 0.97), 2),
                    },
                    "vision": {
                        "model_id": "garbage-classifier-v1",
                        "camera_state": self.camera_state,
                        "confidence": round(random.uniform(0.80, 0.97), 2),
                        "last_inference_at": _utcnow(),
                    },
                    "health": {
                        "device_status": "ONLINE",
                        "rssi_dbm": self.rssi,
                        "uptime_sec": self.uptime_sec,
                        "cpu_temp_c": round(random.uniform(42.0, 58.0), 1),
                        "free_disk_mb": random.randint(1800, 2500),
                        "offline_queue_count": 0,
                        "sensor_faults": [],
                        "camera_fault": False,
                    },
                }
            ],
        }

    def _build_heartbeat_payload(self) -> dict:
        return {
            "schema_version": "1.0",
            "message_id": str(uuid4()),
            "device_id": self.device_id,
            "container_id": self.container_id,
            "sent_at": _utcnow(),
            "seq": self.seq,
            "status": "ONLINE",
            "firmware": {
                "mcu_version": "0.1.0",
                "linux_app_version": "0.1.0",
                "model_id": "garbage-classifier-v1",
            },
            "health": {
                "uptime_sec": self.uptime_sec,
                "rssi_dbm": self.rssi,
                "cpu_temp_c": round(random.uniform(42.0, 58.0), 1),
                "free_disk_mb": random.randint(1800, 2500),
                "offline_queue_count": 0,
                "last_sensor_sample_at": _utcnow(),
                "last_camera_frame_at": _utcnow(),
                "last_successful_upload_at": _utcnow(),
            },
        }

    async def _send_garbage_detected(self, client: httpx.AsyncClient) -> None:
        payload = {
            "schema_version": "1.0",
            "message_id": str(uuid4()),
            "device_id": self.device_id,
            "container_id": self.container_id,
            "sent_at": _utcnow(),
            "seq": self.seq,
            "event": {
                "type": "GARBAGE_DETECTED",
                "severity": "WARNING",
                "started_at": _utcnow(),
                "ended_at": None,
                "confidence": round(random.uniform(0.80, 0.96), 2),
                "summary": "Garbage detected around the container.",
                "state": {
                    "camera_state": "GARBAGE_DETECTED",
                    "fused_fill_pct": round(self.fill_pct, 1),
                    "fill_state": _fill_state(self.fill_pct),
                },
                "evidence": {"image_available": False, "local_image_id": None},
            },
        }
        try:
            resp = await client.post(
                f"{self.backend_url}/api/v1/device/events",
                json=payload,
                headers=self._headers(),
            )
            if resp.status_code == 201:
                data = resp.json()
                self.garbage_event_id = data.get("event_id")
                logger.info(
                    "[sim-%02d] GARBAGE_DETECTED event created: %s",
                    self.sim_index,
                    self.garbage_event_id,
                )
        except Exception as exc:
            logger.warning("[sim-%02d] Failed to send GARBAGE_DETECTED: %s", self.sim_index, exc)

    async def _send_garbage_cleared(self, client: httpx.AsyncClient) -> None:
        payload = {
            "schema_version": "1.0",
            "message_id": str(uuid4()),
            "device_id": self.device_id,
            "container_id": self.container_id,
            "sent_at": _utcnow(),
            "seq": self.seq,
            "event": {
                "type": "GARBAGE_CLEARED",
                "severity": "INFO",
                "started_at": _utcnow(),
                "ended_at": None,
                "confidence": round(random.uniform(0.85, 0.97), 2),
                "summary": "Camera state returned to everything OK.",
                "state": {
                    "camera_state": "EVERYTHING_OK",
                    "fused_fill_pct": round(self.fill_pct, 1),
                    "fill_state": _fill_state(self.fill_pct),
                },
                "evidence": {"image_available": False, "local_image_id": None},
            },
        }
        try:
            resp = await client.post(
                f"{self.backend_url}/api/v1/device/events",
                json=payload,
                headers=self._headers(),
            )
            if resp.status_code == 201:
                logger.info("[sim-%02d] GARBAGE_CLEARED event sent.", self.sim_index)
                self.garbage_event_id = None
        except Exception as exc:
            logger.warning("[sim-%02d] Failed to send GARBAGE_CLEARED: %s", self.sim_index, exc)

    async def telemetry_loop(self) -> None:
        interval = self._real_interval(self.config.get("telemetry_interval_sec", 60))
        prev_camera_state = self.camera_state

        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                if self.paused:
                    await asyncio.sleep(interval)
                    continue

                self._advance_state()

                # Fire camera events on state transitions
                if prev_camera_state == "EVERYTHING_OK" and self.camera_state == "GARBAGE_DETECTED":
                    await self._send_garbage_detected(client)
                elif prev_camera_state == "GARBAGE_DETECTED" and self.camera_state == "EVERYTHING_OK":
                    await self._send_garbage_cleared(client)
                prev_camera_state = self.camera_state

                payload = self._build_telemetry_payload()
                try:
                    resp = await client.post(
                        f"{self.backend_url}/api/v1/device/telemetry",
                        json=payload,
                        headers=self._headers(),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("config_revision", 0) > self.config_revision:
                            self.config_revision = data["config_revision"]
                            logger.info(
                                "[sim-%02d] New config revision %d available.",
                                self.sim_index,
                                self.config_revision,
                            )
                        logger.debug(
                            "[sim-%02d] telemetry sent — fill=%.1f%% camera=%s",
                            self.sim_index,
                            self.fill_pct,
                            self.camera_state,
                        )
                    else:
                        logger.warning(
                            "[sim-%02d] Telemetry returned %d: %s",
                            self.sim_index,
                            resp.status_code,
                            resp.text[:200],
                        )
                except Exception as exc:
                    logger.warning("[sim-%02d] Telemetry error: %s", self.sim_index, exc)

                await asyncio.sleep(interval)

    async def heartbeat_loop(self) -> None:
        interval = self._real_interval(self.config.get("heartbeat_interval_sec", 60))
        # Offset heartbeats so they don't all fire at the same moment
        await asyncio.sleep(interval * 0.5)

        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                if self.paused:
                    await asyncio.sleep(interval)
                    continue

                payload = self._build_heartbeat_payload()
                try:
                    await client.post(
                        f"{self.backend_url}/api/v1/device/heartbeat",
                        json=payload,
                        headers=self._headers(),
                    )
                    logger.debug("[sim-%02d] heartbeat sent.", self.sim_index)
                except Exception as exc:
                    logger.warning("[sim-%02d] Heartbeat error: %s", self.sim_index, exc)

                await asyncio.sleep(interval)

    async def run(self) -> None:
        logger.info(
            "[sim-%02d] Starting device=%s container=%s fill=%.0f%%",
            self.sim_index,
            self.device_id,
            self.container_id,
            self.fill_pct,
        )
        await asyncio.gather(self.telemetry_loop(), self.heartbeat_loop())
