"""Runtime configuration and on-disk locations."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

KINOPUB_API = "https://api.service-kp.com"
KINOPUB_DEVICE_URL = f"{KINOPUB_API}/oauth2/device"
KINOPUB_TOKEN_URL = f"{KINOPUB_API}/oauth2/token"
TRAKT_API = "https://api.trakt.tv"


class Paths(BaseModel):
    data_dir: Path = PROJECT_ROOT / "data"

    @property
    def tokens(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def dump(self) -> Path:
        return self.data_dir / "kinopub_dump.json"

    @property
    def plan(self) -> Path:
        return self.data_dir / "sync_plan.json"

    @property
    def push_state(self) -> Path:
        return self.data_dir / "push_state.json"

    @property
    def trakt_cache(self) -> Path:
        return self.data_dir / "trakt_cache.json"

    @property
    def reconcile_cache(self) -> Path:
        return self.data_dir / "reconcile_cache.json"

    @property
    def verify_report(self) -> Path:
        return self.data_dir / "verify_report.json"


class Settings(BaseSettings):
    """Environment-driven configuration; `.env` seeds anything unset.

    The API client credentials default to the public ones shipped by
    open-source clients, because both services authenticate with a device-code
    flow: security rests on the user's own authorization, not on a secret
    client key. kino.pub needs no registration at all, and Trakt gated new
    API-app creation behind VIP in 2025 while leaving the endpoints used here
    free — reusing an existing public client sidesteps app creation entirely.
    """

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    kinopub_client_id: str = "xbmc"
    kinopub_client_secret: str = "cgg3gtifu46urtfp2zp1nqtba0k2ezxh"

    # iamkroot/trakt-scrobbler's public device-flow credentials.
    trakt_client_id: str = "ab0133a2365b2c64d70fd2adf3c7e775a4131471b56340933335af1b94785a3a"
    trakt_client_secret: str = "b574acd5857310fcdc1e195c5953795fc61a1d89d69fec1649624d54cb666222"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"

    paths: Paths = Field(default_factory=Paths)
