"""
Preset demo scenarios.  Each function receives the list of DeviceSimulator
instances and manipulates their state or pauses them to create interesting
demo conditions.  Scenarios run as a background asyncio task alongside the
normal device loops.
"""

import asyncio
import logging
import random

logger = logging.getLogger(__name__)


async def normal_operation(devices) -> None:
    """All devices run with gradual fill increase. No intervention."""
    logger.info("[scenario] normal_operation — all %d devices running.", len(devices))
    # Nothing to do: device loops handle everything.
    await asyncio.sleep(float("inf"))


async def garbage_detection(devices) -> None:
    """Force device[0] to trigger GARBAGE_DETECTED immediately."""
    if not devices:
        return
    target = devices[0]
    logger.info(
        "[scenario] garbage_detection — forcing GARBAGE_DETECTED on %s in 5s.",
        target.device_id,
    )
    await asyncio.sleep(5)
    target.camera_state = "GARBAGE_DETECTED"
    target.garbage_clear_ticks = 6
    logger.info("[scenario] GARBAGE_DETECTED triggered on %s.", target.device_id)
    await asyncio.sleep(float("inf"))


async def device_goes_offline(devices) -> None:
    """Pause device[-1] for 12 minutes of real time to trigger DEVICE_OFFLINE."""
    if len(devices) < 2:
        return
    target = devices[-1]
    logger.info(
        "[scenario] device_goes_offline — pausing %s for 12 simulated minutes.",
        target.device_id,
    )
    await asyncio.sleep(10)
    target.paused = True
    logger.info("[scenario] %s is now paused (simulating offline).", target.device_id)

    # 12 simulated minutes / speed_factor
    offline_real_secs = (12 * 60) / target.speed_factor
    await asyncio.sleep(offline_real_secs)

    target.paused = False
    logger.info("[scenario] %s back online.", target.device_id)
    await asyncio.sleep(float("inf"))


async def fill_critical(devices) -> None:
    """Accelerate device[1] fill rate to reach CRITICAL quickly."""
    if len(devices) < 2:
        return
    target = devices[1]
    logger.info(
        "[scenario] fill_critical — accelerating fill on %s.", target.device_id
    )
    await asyncio.sleep(3)
    target.fill_rate = 15.0  # fills very fast
    logger.info("[scenario] %s fill rate set to 15%%/min.", target.device_id)
    await asyncio.sleep(float("inf"))


async def full_cycle(devices) -> None:
    """
    Stagger devices so they each fill up at different rates, giving the
    dashboard a realistic mix of states for a demo route-planning flow.
    """
    logger.info("[scenario] full_cycle — staggering fill rates across %d devices.", len(devices))
    rates = [1.0, 2.5, 4.0, 6.0, 10.0]
    starting_fills = [10.0, 25.0, 45.0, 60.0, 80.0]

    for i, device in enumerate(devices):
        device.fill_rate = rates[i % len(rates)]
        device.fill_pct = starting_fills[i % len(starting_fills)]
        logger.info(
            "[scenario] %s — fill=%.0f%% rate=%.1f%%/min",
            device.device_id,
            device.fill_pct,
            device.fill_rate,
        )

    await asyncio.sleep(float("inf"))


SCENARIOS = {
    "normal_operation": normal_operation,
    "garbage_detection": garbage_detection,
    "device_goes_offline": device_goes_offline,
    "fill_critical": fill_critical,
    "full_cycle": full_cycle,
}
