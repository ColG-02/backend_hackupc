import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level up from simulator/)
load_dotenv(Path(__file__).parent.parent / ".env")

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8080")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@smart-waste.local")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin-change-this")

# Site used for all simulated containers
SIM_SITE_ID = "site-sim-demo"

# Belgrade city centre as default depot
DEPOT_LAT = 44.8176
DEPOT_LNG = 20.4573

# Simulated container locations (lat/lng pairs)
CONTAINER_LOCATIONS = [
    (44.8176, 20.4573),
    (44.8200, 20.4612),
    (44.8150, 20.4530),
    (44.8230, 20.4480),
    (44.8100, 20.4660),
    (44.8260, 20.4700),
    (44.8080, 20.4490),
    (44.8310, 20.4410),
    (44.8050, 20.4740),
    (44.8350, 20.4350),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smart-waste device simulator")
    parser.add_argument(
        "--devices",
        type=int,
        default=5,
        help="Number of simulated devices (default: 5)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=60.0,
        help="Speed factor: simulated minutes per real second (default: 60)",
    )
    parser.add_argument(
        "--scenario",
        choices=["normal_operation", "garbage_detection", "device_goes_offline", "fill_critical", "full_cycle"],
        default="normal_operation",
        help="Demo scenario to run (default: normal_operation)",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default=BACKEND_URL,
        help=f"Backend base URL (default: {BACKEND_URL})",
    )
    return parser.parse_args()
