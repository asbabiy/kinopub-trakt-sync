"""Trakt API v2 client: device-code auth, history/watchlist sync, scrobble.

API reference: https://trakt.docs.apiary.io/ (base https://api.trakt.tv).
POST endpoints are rate-limited to 1 call per second.
"""

import time

import httpx

from . import config


class TraktError(RuntimeError):
    pass


class TraktClient:
    def __init__(self) -> None:
        if not config.TRAKT_CLIENT_ID or not config.TRAKT_CLIENT_SECRET:
            raise TraktError(
                "TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET not set. Create an app at "
                "https://trakt.tv/oauth/applications and put both into .env"
            )
        self._http = httpx.Client(base_url=config.TRAKT_API, timeout=30)

    # -- auth ------------------------------------------------------------

    def device_auth(self) -> None:
        resp = self._http.post("/oauth/device/code", json={"client_id": config.TRAKT_CLIENT_ID})
        resp.raise_for_status()
        data = resp.json()
        print(f"Open {data['verification_url']} and enter code: {data['user_code']}")
        deadline = time.time() + data.get("expires_in", 600)
        interval = data.get("interval", 5)
        while time.time() < deadline:
            time.sleep(interval)
            resp = self._http.post(
                "/oauth/device/token",
                json={
                    "code": data["device_code"],
                    "client_id": config.TRAKT_CLIENT_ID,
                    "client_secret": config.TRAKT_CLIENT_SECRET,
                },
            )
            if resp.status_code == 200:
                self._store(resp.json())
                print("trakt: authorized")
                return
            if resp.status_code == 429:
                interval += 1
                continue
            if resp.status_code != 400:  # 400 = authorization pending
                raise TraktError(f"device auth failed: {resp.status_code} {resp.text}")
        raise TraktError("device code expired, run auth again")

    def _store(self, payload: dict) -> None:
        tokens = config.load_tokens()
        payload["obtained_at"] = int(time.time())
        tokens["trakt"] = payload
        config.save_tokens(tokens)

    def _access_token(self) -> str:
        tokens = config.load_tokens().get("trakt")
        if not tokens:
            raise TraktError("not authorized, run: kts auth trakt")
        if time.time() > tokens["obtained_at"] + tokens.get("expires_in", 86400) - 300:
            resp = self._http.post(
                "/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": config.TRAKT_CLIENT_ID,
                    "client_secret": config.TRAKT_CLIENT_SECRET,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                },
            )
            if resp.status_code != 200:
                raise TraktError(f"token refresh failed ({resp.status_code}), run: kts auth trakt")
            self._store(resp.json())
            tokens = config.load_tokens()["trakt"]
        return tokens["access_token"]

    # -- requests --------------------------------------------------------

    def _request(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        headers = {
            "trakt-api-version": "2",
            "trakt-api-key": config.TRAKT_CLIENT_ID,
            "Authorization": f"Bearer {self._access_token()}",
        }
        for _ in range(5):
            resp = self._http.request(method, path, json=json_body, headers=headers)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 2)))
                continue
            return resp
        raise TraktError(f"rate limited repeatedly on {path}")

    def watched(self, media: str) -> list:
        resp = self._request("GET", f"/sync/watched/{media}")
        resp.raise_for_status()
        return resp.json()

    def sync_history(self, body: dict) -> dict:
        resp = self._request("POST", "/sync/history", body)
        resp.raise_for_status()
        return resp.json()

    def sync_watchlist(self, body: dict) -> dict:
        resp = self._request("POST", "/sync/watchlist", body)
        resp.raise_for_status()
        return resp.json()

    def scrobble_pause(self, body: dict) -> str:
        """Saves playback progress. Returns outcome: ok | duplicate | rejected."""
        resp = self._request("POST", "/scrobble/pause", body)
        if resp.status_code == 409:
            return "duplicate"
        if resp.status_code == 422:
            return "rejected"
        resp.raise_for_status()
        return "ok"
