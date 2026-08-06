import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

TOKENS_FILE = DATA_DIR / "tokens.json"
DUMP_FILE = DATA_DIR / "kinopub_dump.json"
PLAN_FILE = DATA_DIR / "sync_plan.json"
STATE_FILE = DATA_DIR / "push_state.json"


def _load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

KINOPUB_API = "https://api.service-kp.com"
KINOPUB_DEVICE_URL = f"{KINOPUB_API}/oauth2/device"
KINOPUB_TOKEN_URL = f"{KINOPUB_API}/oauth2/token"
# Public client credentials shared by open-source kino.pub clients (Kodi, webOS, Roku).
KINOPUB_CLIENT_ID = os.environ.get("KINOPUB_CLIENT_ID", "xbmc")
KINOPUB_CLIENT_SECRET = os.environ.get("KINOPUB_CLIENT_SECRET", "cgg3gtifu46urtfp2zp1nqtba0k2ezxh")

TRAKT_API = "https://api.trakt.tv"
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "")
TRAKT_CLIENT_SECRET = os.environ.get("TRAKT_CLIENT_SECRET", "")


def load_tokens() -> dict:
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return {}


def save_tokens(tokens: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    TOKENS_FILE.chmod(0o600)


def save_json(path: Path, payload: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())
