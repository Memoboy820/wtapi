import logging
from pathlib import Path
import re
from dotenv import load_dotenv

__all__ = [
    "ReplayAPIError",
    "ReplayDownloadError",
    "normalize_session_id",
    "fetch_replays",
    "save_replay_response",
    "download_replay",
]



PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_FOLDER = PROJECT_ROOT / "data"
REPLAY_FOLDER = DATA_FOLDER / "replays"
DB_PATH = DATA_FOLDER / "teams" / "teams.db"
DB_PATH = Path(DB_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

API_URL = "https://warthunder.com/ru/api/replay"
BASE_URL = "https://wt-game-replays.warthunder.com"

HEADER_SIZE = 1234
SIGNATURE = 0x1000ACE5
MAX_SESSION_ID = (1 << 64) - 1
RETRY_STATUSES = {429, 500, 502, 503, 504}

logger = logging.getLogger(__name__)



PARAMS = {
    "findMissionValue": "",
    "findUserType": "USERNAME",
    "findUserValue": "",
    "gameMode": [
        "arcade",
        "realistic",
        "simulation",
    ],
    "gameType": "clanBattle",
    "isUserOwnReplays": False,
    "limit": 20,
    "page": 1,
    "rankRange": "",
    "techType": "all",
    "timeRangeFrom": "",
    "timeRangeFromDay": 8,
    "timeRangeFromMonth": 2,
    "timeRangeFromTime": "10:00",
    "timeRangeTo": "",
    "timeRangeToDay": 10,
    "timeRangeToMonth": 5,
    "timeRangeToTime": "14:00",
}



DATA_FOLDER = PROJECT_ROOT / "data"
STATE_FILE = DATA_FOLDER / "replay_state.json"
API_RESPONSE_FILE = DATA_FOLDER / "replay_response.json"

REPLAY_FOLDER = PROJECT_ROOT / "data" / "replays"

CHECK_INTERVAL = 10
BATTLE_LIMIT = 20

# макс скачиваний за рз
DOWNLOAD_CONCURRENCY = 3

REQUEST_DELAY = 2

__all__ = [
    "ReplayServiceError",
    "get_vehicle_name",
    "parse_wrpl_folder",
    "parse_wrpl_files",
    "parse_wrpl_json",
]


HEADER_SIZE = 1234
SIGNATURE = 0x1000ACE5
MAX_OUTPUT = 64 * 1024 * 1024

SLOT_PREFIX = bytes.fromhex("02 58 2d f0")

FIELD_SIZES = {
    1: 1,
    2: 8,
    3: 16,
    4: 32,
    5: 64,
    6: 96,
    7: 128,
}

MODEL_PATTERN = re.compile(
    rb"([A-Za-z0-9_/]+)"
    rb"([\x01-\x7f])"
    rb"(t1_player[0-9]+_[0-9]+(?:_support_[0-9]+)?)"
)