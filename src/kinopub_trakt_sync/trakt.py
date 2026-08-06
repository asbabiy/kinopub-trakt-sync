"""Trakt API v2 client: device-code auth, history/watchlist sync, scrobble.

API reference: https://trakt.docs.apiary.io/ (base https://api.trakt.tv).
GETs run concurrently (bounded); POSTs are serialized at ~1 rps because Trakt
hard-limits write calls to 1 per second — parallelism there only buys 429s.
"""

import asyncio
import time

import httpx

from . import config

GET_CONCURRENCY = 8


class TraktError(RuntimeError):
    pass


class TraktClient:
    def __init__(self) -> None:
        if not config.TRAKT_CLIENT_ID or not config.TRAKT_CLIENT_SECRET:
            raise TraktError(
                "TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET not set. Create an app at "
                "https://trakt.tv/oauth/applications and put both into .env"
            )
        self._http = httpx.AsyncClient(base_url=config.TRAKT_API, timeout=30)
        self._sem = asyncio.Semaphore(GET_CONCURRENCY)

    # -- auth ------------------------------------------------------------

    async def device_auth(self) -> None:
        resp = await self._http.post("/oauth/device/code", json={"client_id": config.TRAKT_CLIENT_ID})
        resp.raise_for_status()
        data = resp.json()
        print(f"Open {data['verification_url']} and enter code: {data['user_code']}")
        deadline = time.time() + data.get("expires_in", 600)
        interval = data.get("interval", 5)
        while time.time() < deadline:
            await asyncio.sleep(interval)
            resp = await self._http.post(
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

    async def _access_token(self) -> str:
        tokens = config.load_tokens().get("trakt")
        if not tokens:
            raise TraktError("not authorized, run: kts auth trakt")
        if time.time() > tokens["obtained_at"] + tokens.get("expires_in", 86400) - 300:
            resp = await self._http.post(
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

    async def _request(
        self, method: str, path: str, json_body: dict | None = None, params: dict | None = None
    ) -> httpx.Response:
        headers = {
            "trakt-api-version": "2",
            "trakt-api-key": config.TRAKT_CLIENT_ID,
            "Authorization": f"Bearer {await self._access_token()}",
        }
        for _ in range(5):
            async with self._sem:
                resp = await self._http.request(
                    method, path, json=json_body, params=params, headers=headers
                )
            if resp.status_code == 429:
                await asyncio.sleep(int(resp.headers.get("Retry-After", 2)))
                continue
            return resp
        raise TraktError(f"rate limited repeatedly on {path}")

    async def get_json(self, path: str, **params):
        """GET returning parsed JSON, or None on 404."""
        resp = await self._request("GET", path, params=params or None)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def paginated(self, path: str, **params) -> list:
        """All pages; the first page reveals the page count, the rest fetch
        concurrently."""
        first = await self._request("GET", path, params={**params, "page": 1, "limit": 100})
        first.raise_for_status()
        rows = first.json()
        pages = int(first.headers.get("x-pagination-page-count") or 1)
        if pages > 1:
            results = await asyncio.gather(
                *[
                    self._request("GET", path, params={**params, "page": p, "limit": 100})
                    for p in range(2, pages + 1)
                ]
            )
            for resp in results:
                resp.raise_for_status()
                rows.extend(resp.json())
        return rows

    async def watched(self, media: str) -> list:
        resp = await self._request("GET", f"/sync/watched/{media}")
        resp.raise_for_status()
        return resp.json()

    async def sync_history(self, body: dict) -> dict:
        resp = await self._request("POST", "/sync/history", body)
        resp.raise_for_status()
        return resp.json()

    async def history_remove(self, event_ids: list) -> dict:
        resp = await self._request("POST", "/sync/history/remove", {"ids": event_ids})
        resp.raise_for_status()
        return resp.json()

    async def sync_watchlist(self, body: dict) -> dict:
        resp = await self._request("POST", "/sync/watchlist", body)
        resp.raise_for_status()
        return resp.json()

    async def scrobble_pause(self, body: dict) -> str:
        """Saves playback progress. Returns outcome: ok | duplicate | rejected."""
        resp = await self._request("POST", "/scrobble/pause", body)
        if resp.status_code == 409:
            return "duplicate"
        if resp.status_code == 422:
            return "rejected"
        resp.raise_for_status()
        return "ok"
