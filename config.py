from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).parent

# Data directory (gitignored)
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Database
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/transit.db")

# GTFS Static
# Obtain feed URL from: https://www.metrolinx.com/en/go-transit/about-go-transit/open-data
GTFS_STATIC_URL: str = os.getenv("GTFS_STATIC_URL", "")
GTFS_REFRESH_HOURS: int = int(os.getenv("GTFS_REFRESH_HOURS", "24"))

# GTFS-Realtime
GTFS_RT_TRIP_UPDATES_URL: str = os.getenv("GTFS_RT_TRIP_UPDATES_URL", "")
GTFS_RT_VEHICLE_POSITIONS_URL: str = os.getenv("GTFS_RT_VEHICLE_POSITIONS_URL", "")
GTFS_RT_ALERTS_URL: str = os.getenv("GTFS_RT_ALERTS_URL", "")
GTFS_RT_API_KEY: str = os.getenv("GTFS_RT_API_KEY", "")  # appended as ?key= on each RT request
GTFS_RT_POLL_SECONDS: int = int(os.getenv("GTFS_RT_POLL_SECONDS", "30"))

# LLM (local Ollama — https://ollama.com)
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.2")

# API
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

# Routing constraints
MAX_ROUTES: int = int(os.getenv("MAX_ROUTES", "5"))
MAX_TRANSFERS: int = int(os.getenv("MAX_TRANSFERS", "2"))
MIN_TRANSFER_MINUTES: int = int(os.getenv("MIN_TRANSFER_MINUTES", "10"))
# Walking transfer
MAX_WALK_METRES: int = int(os.getenv("MAX_WALK_METRES", "500"))
WALK_SPEED_KPH: float = float(os.getenv("WALK_SPEED_KPH", "4.5"))
