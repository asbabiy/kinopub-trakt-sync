"""Trakt API v2 client: device-code auth, history/watchlist sync, scrobble.

API reference: https://trakt.docs.apiary.io/ (base https://api.trakt.tv).
GETs run concurrently; writes are serialized at ~1 rps by the call sites,
because Trakt hard-limits POST/PUT/DELETE to one per second — parallelism there
only buys 429s.
"""

from __future__ import annotations

import asyncio
import logging
import time
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .settings import TRAKT_API, Settings
from .storage import read_json, write_secret_json

log = logging.getLogger(__name__)

GET_CONCURRENCY = 8
PAGE_SIZE = 100
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER = 2
POLL_TIMEOUT_SECONDS = 600


class TraktError(RuntimeError):
    pass


class TraktClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=TRAKT_API, timeout=30)
        self._semaphore = asyncio.Semaphore(GET_CONCURRENCY)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    # -- auth ------------------------------------------------------------

    async def device_auth(self) -> None:
        response = await self._http.post(
            "/oauth/device/code", json={"client_id": self._settings.trakt_client_id}
        )
        response.raise_for_status()
        device = response.json()
        print(f"Open {device['verification_url']} and enter code: {device['user_code']}")

        deadline = time.time() + device.get("expires_in", POLL_TIMEOUT_SECONDS)
        interval = device.get("interval", 5)
        while time.time() < deadline:
            await asyncio.sleep(interval)
            response = await self._http.post(
                "/oauth/device/token",
                json={
                    "code": device["device_code"],
                    "client_id": self._settings.trakt_client_id,
                    "client_secret": self._settings.trakt_client_secret,
                },
            )
            if response.status_code == HTTPStatus.OK:
                self._store_tokens(response.json())
                print("trakt: authorized")
                return
            if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                interval += 1
            elif response.status_code != HTTPStatus.BAD_REQUEST:  # 400 = still pending
                raise TraktError(f"device auth failed: {response.status_code} {response.text}")
        raise TraktError("device code expired, run auth again")

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        tokens = read_json(self._settings.paths.tokens, default={})
        tokens["trakt"] = {**payload, "obtained_at": int(time.time())}
        write_secret_json(self._settings.paths.tokens, tokens)

    async def _access_token(self) -> str:
        tokens = read_json(self._settings.paths.tokens, default={}).get("trakt")
        if not tokens:
            raise TraktError("not authorized, run: kts auth trakt")
        if time.time() <= tokens["obtained_at"] + tokens.get("expires_in", 86400) - 300:
            return tokens["access_token"]

        response = await self._http.post(
            "/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self._settings.trakt_client_id,
                "client_secret": self._settings.trakt_client_secret,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            },
        )
        if response.status_code != HTTPStatus.OK:
            raise TraktError(f"token refresh failed ({response.status_code}), run: kts auth trakt")
        self._store_tokens(response.json())
        return read_json(self._settings.paths.tokens)["trakt"]["access_token"]

    # -- transport -------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Transport failures are retried with backoff; throttling is not —
        Trakt states the exact delay in Retry-After, which beats guessing."""
        headers = {
            "trakt-api-version": "2",
            "trakt-api-key": self._settings.trakt_client_id,
            "Authorization": f"Bearer {await self._access_token()}",
        }
        for _ in range(MAX_RATE_LIMIT_RETRIES):
            async with self._semaphore:
                response = await self._http.request(
                    method, path, json=json_body, params=params, headers=headers
                )
            if response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
                return response
            delay = int(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER))
            log.debug("trakt throttled %s, waiting %ss", path, delay)
            await asyncio.sleep(delay)
        raise TraktError(f"rate limited repeatedly on {path}")

    async def get_json(self, path: str, **params: Any) -> Any:
        """Parsed JSON, or None when Trakt does not know the resource."""
        response = await self.request("GET", path, params=params or None)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.json()

    async def paginated(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """All pages: the first response reveals the page count, the rest are
        fetched concurrently."""
        first = await self.request("GET", path, params={**params, "page": 1, "limit": PAGE_SIZE})
        first.raise_for_status()
        rows: list[dict[str, Any]] = list(first.json())
        page_count = int(first.headers.get("x-pagination-page-count") or 1)
        if page_count > 1:
            responses = await asyncio.gather(
                *[
                    self.request("GET", path, params={**params, "page": page, "limit": PAGE_SIZE})
                    for page in range(2, page_count + 1)
                ]
            )
            for response in responses:
                response.raise_for_status()
                rows.extend(response.json())
        return rows

    # -- sync ------------------------------------------------------------

    async def watched(self, media: str) -> list[dict[str, Any]]:
        response = await self.request("GET", f"/sync/watched/{media}")
        response.raise_for_status()
        return response.json()

    async def playback(self) -> list[dict[str, Any]]:
        return await self.get_json("/sync/playback") or []

    async def add_to_history(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("POST", "/sync/history", json_body=body)
        response.raise_for_status()
        return response.json()

    async def remove_from_history(self, event_ids: list[int]) -> dict[str, Any]:
        response = await self.request("POST", "/sync/history/remove", json_body={"ids": event_ids})
        response.raise_for_status()
        return response.json()

    async def add_to_watchlist(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("POST", "/sync/watchlist", json_body=body)
        response.raise_for_status()
        return response.json()

    async def scrobble_pause(self, body: dict[str, Any]) -> str:
        """Store playback progress. Returns ok | duplicate | rejected.

        Trakt ignores a pause below 1% and at or above its 80% "finished"
        threshold, so `rejected` is an expected, non-fatal outcome.
        """
        response = await self.request("POST", "/scrobble/pause", json_body=body)
        if response.status_code == HTTPStatus.CONFLICT:
            return "duplicate"
        if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            return "rejected"
        response.raise_for_status()
        return "ok"
