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
# Trakt gated new API-app creation behind VIP in early 2025, but the device-code
# flow needs no per-user app: security comes from the user's own authorization,
# not from client_secret being secret. These are the public credentials shipped
# by the open-source iamkroot/trakt-scrobbler client (base64 in its
# trakt_key_holder.py), a multi-user device-flow scrobbler. The endpoints we call
# (/sync/history, /scrobble/pause, /sync/watchlist) are free, not VIP-only.
# Override with your own app in .env if you have one.
TRAKT_CLIENT_ID = os.environ.get(
    "TRAKT_CLIENT_ID",
    "ab0133a2365b2c64d70fd2adf3c7e775a4131471b56340933335af1b94785a3a",
)
TRAKT_CLIENT_SECRET = os.environ.get(
    "TRAKT_CLIENT_SECRET",
    "b574acd5857310fcdc1e195c5953795fc61a1d89d69fec1649624d54cb666222",
)


def load_tokens() -> dict:
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return {}


def save_tokens(tokens: dict) -> None:
    save_json(TOKENS_FILE, tokens)
    TOKENS_FILE.chmod(0o600)


def save_json(path: Path, payload: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    # Atomic write so an interrupted checkpoint can't leave a truncated file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    tmp.replace(path)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())
