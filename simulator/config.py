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

# Barcelona city centre as depot
DEPOT_LAT = 41.3851
DEPOT_LNG = 2.1734

# Simulated container locations (lat/lng pairs) — spread across Barcelona
CONTAINER_LOCATIONS = [
    (41.3851, 2.1734),   # Eixample centre
    (41.3879, 2.1699),   # Passeig de Gràcia
    (41.3808, 2.1761),   # Sant Antoni
    (41.3902, 2.1540),   # Les Corts
    (41.3763, 2.1863),   # Barceloneta
    (41.4032, 2.1743),   # Gràcia
    (41.3750, 2.1490),   # Sants
    (41.4105, 2.1524),   # Sarrià
    (41.3938, 2.1940),   # Poblenou
    (41.3695, 2.1780),   # Port Olímpic area
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
