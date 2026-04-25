"""
Simulator entry point.

Usage:
    python -m simulator.main --devices 5 --speed 60 --scenario normal_operation
    python -m simulator.main --devices 8 --speed 120 --scenario full_cycle
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx

# Allow running as `python simulator/main.py` from the backend root
sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator.config import (
    CONTAINER_LOCATIONS,
    DEPOT_LAT,
    DEPOT_LNG,
    SIM_SITE_ID,
    parse_args,
)
from simulator.device_sim import DeviceSimulator
from simulator.scenarios import SCENARIOS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

STATE_STORE_PATH = Path(__file__).parent / "state_store.json"


def _load_state() -> list[dict]:
    if STATE_STORE_PATH.exists():
        with open(STATE_STORE_PATH) as f:
            return json.load(f).get("devices", [])
    return []


def _save_state(devices: list[dict]) -> None:
    with open(STATE_STORE_PATH, "w") as f:
        json.dump({"devices": devices}, f, indent=2)


async def _get_admin_token(backend_url: str, email: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{backend_url}/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _create_container(
    client: httpx.AsyncClient,
    backend_url: str,
    jwt: str,
    container_id: str,
    index: int,
) -> None:
    lat, lng = CONTAINER_LOCATIONS[index % len(CONTAINER_LOCATIONS)]
    payload = {
        "container_id": container_id,
        "name": f"Simulated container {index + 1:02d}",
        "site_id": SIM_SITE_ID,
        "location": {"type": "Point", "coordinates": [lng, lat]},
        "address": f"Sim Street {index + 1}",
        "container_type": "UNDERGROUND",
        "capacity": {"volume_l": 3000, "max_payload_kg": 400},
    }
    resp = await client.post(
        f"{backend_url}/api/v1/containers",
        json=payload,
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if resp.status_code not in (201, 409):
        resp.raise_for_status()


async def _create_claim_code(
    client: httpx.AsyncClient,
    backend_url: str,
    jwt: str,
    container_id: str,
    code: str,
) -> None:
    resp = await client.post(
        f"{backend_url}/api/v1/devices/claim-codes",
        json={"container_id": container_id, "code": code},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if resp.status_code not in (201, 409):
        resp.raise_for_status()


async def _bootstrap_device(
    client: httpx.AsyncClient,
    backend_url: str,
    factory_id: str,
    claim_code: str,
) -> dict:
    payload = {
        "schema_version": "1.0",
        "factory_device_id": factory_id,
        "claim_code": claim_code,
        "firmware": {
            "mcu_version": "0.1.0",
            "linux_app_version": "0.1.0",
            "model_id": "garbage-classifier-v1",
        },
        "capabilities": {
            "sensors": ["temperature", "humidity", "light", "ultrasonic", "weight", "tamper"],
            "camera": True,
            "offline_buffer": True,
        },
    }
    resp = await client.post(f"{backend_url}/api/v1/device/bootstrap", json=payload)
    resp.raise_for_status()
    return resp.json()


async def _provision_devices(
    backend_url: str, admin_email: str, admin_password: str, num_devices: int
) -> list[dict]:
    """Bootstrap any devices not yet in state_store and return full list."""
    stored = _load_state()
    stored_by_index = {d["sim_index"]: d for d in stored}

    jwt = await _get_admin_token(backend_url, admin_email, admin_password)
    logger.info("Admin login successful.")

    provisioned: list[dict] = list(stored)

    async with httpx.AsyncClient(timeout=15.0) as client:
        for i in range(num_devices):
            if i in stored_by_index:
                logger.info("[sim-%02d] Loaded from state_store.", i)
                continue

            container_id = f"bin-sim-{i:03d}"
            factory_id = f"unoq-sim-{i:03d}"
            claim_code = f"SIM-{i:04d}-AUTO"

            await _create_container(client, backend_url, jwt, container_id, i)
            await _create_claim_code(client, backend_url, jwt, container_id, claim_code)

            bootstrap = await _bootstrap_device(client, backend_url, factory_id, claim_code)
            entry = {
                "sim_index": i,
                "factory_device_id": factory_id,
                "device_id": bootstrap["device_id"],
                "container_id": bootstrap["container_id"],
                "device_token": bootstrap["device_token"],
                "config": bootstrap.get("config", {}),
                "config_revision": bootstrap.get("config_revision", 1),
            }
            provisioned.append(entry)
            logger.info(
                "[sim-%02d] Bootstrapped: device=%s container=%s",
                i,
                entry["device_id"],
                entry["container_id"],
            )

    # Keep only the N devices we need (supports shrinking num_devices)
    final = [d for d in provisioned if d["sim_index"] < num_devices]
    _save_state(final)
    return final


async def main() -> None:
    from simulator.config import ADMIN_EMAIL, ADMIN_PASSWORD

    args = parse_args()
    backend_url = args.backend

    logger.info(
        "Starting simulator: %d devices, speed=%.0fx, scenario=%s, backend=%s",
        args.devices,
        args.speed,
        args.scenario,
        backend_url,
    )

    device_records = await _provision_devices(
        backend_url, ADMIN_EMAIL, ADMIN_PASSWORD, args.devices
    )

    simulators: list[DeviceSimulator] = []
    for record in device_records:
        i = record["sim_index"]
        sim = DeviceSimulator(
            sim_index=i,
            device_id=record["device_id"],
            container_id=record["container_id"],
            device_token=record["device_token"],
            config=record.get("config", {}),
            backend_url=backend_url,
            speed_factor=args.speed,
            location=CONTAINER_LOCATIONS[i % len(CONTAINER_LOCATIONS)],
        )
        simulators.append(sim)

    scenario_fn = SCENARIOS.get(args.scenario, SCENARIOS["normal_operation"])

    tasks = [asyncio.create_task(sim.run()) for sim in simulators]
    tasks.append(asyncio.create_task(scenario_fn(simulators)))

    logger.info("All %d simulators running. Press Ctrl+C to stop.", len(simulators))
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Simulator stopped by user.")
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
