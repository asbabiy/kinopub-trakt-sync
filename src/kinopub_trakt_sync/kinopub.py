"""kino.pub API client: device-code auth and concurrent read-only extraction.

API reference: https://kinoapi.com/ (base https://api.service-kp.com/v1).
"""

import asyncio
import time

import httpx

from . import config

CONCURRENCY = 16  # kino.pub is a small service; higher risks 429s/bans


class KinopubError(RuntimeError):
    pass


class KinopubClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=30)
        self._sem = asyncio.Semaphore(CONCURRENCY)

    # -- auth ------------------------------------------------------------

    async def device_auth(self) -> None:
        resp = await self._http.post(
            config.KINOPUB_DEVICE_URL,
            data={
                "grant_type": "device_code",
                "client_id": config.KINOPUB_CLIENT_ID,
                "client_secret": config.KINOPUB_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"Open {data['verification_uri']} and enter code: {data['user_code']}")
        deadline = time.time() + data.get("expires_in", 300)
        while time.time() < deadline:
            await asyncio.sleep(data.get("interval", 5))
            resp = await self._http.post(
                config.KINOPUB_DEVICE_URL,
                data={
                    "grant_type": "device_token",
                    "client_id": config.KINOPUB_CLIENT_ID,
                    "client_secret": config.KINOPUB_CLIENT_SECRET,
                    "code": data["code"],
                },
            )
            if resp.status_code == 200:
                self._store(resp.json())
                print("kino.pub: authorized")
                return
            if resp.json().get("error") != "authorization_pending":
                raise KinopubError(resp.text)
        raise KinopubError("device code expired, run auth again")

    def _store(self, payload: dict) -> None:
        tokens = config.load_tokens()
        payload["obtained_at"] = int(time.time())
        tokens["kinopub"] = payload
        config.save_tokens(tokens)

    async def _access_token(self) -> str:
        tokens = config.load_tokens().get("kinopub")
        if not tokens:
            raise KinopubError("not authorized, run: kts auth kinopub")
        if time.time() > tokens["obtained_at"] + tokens.get("expires_in", 3600) - 60:
            resp = await self._http.post(
                config.KINOPUB_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": config.KINOPUB_CLIENT_ID,
                    "client_secret": config.KINOPUB_CLIENT_SECRET,
                    "refresh_token": tokens["refresh_token"],
                },
            )
            if resp.status_code != 200:
                raise KinopubError(f"token refresh failed ({resp.status_code}), run: kts auth kinopub")
            self._store(resp.json())
            tokens = config.load_tokens()["kinopub"]
        return tokens["access_token"]

    # -- data ------------------------------------------------------------

    async def get(self, path: str, **params) -> dict:
        params["access_token"] = await self._access_token()
        async with self._sem:
            resp = await self._http.get(f"{config.KINOPUB_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_optional(self, path: str, **params):
        """Like get(), but returns None on 404 (item deleted from catalog)."""
        try:
            return await self.get(path, **params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    @staticmethod
    def records(page_data: dict) -> list:
        # Response payloads keep the record array under a varying key
        # ("history", "items", ...) next to "pagination"/"status" scalars.
        for key, value in page_data.items():
            if key != "pagination" and isinstance(value, list):
                return value
        return []

    async def history_all(self) -> list:
        first = await self.get("/v1/history", page=1, perpage=50)
        records = self.records(first)
        total = int((first.get("pagination") or {}).get("total") or 1)
        pages = await asyncio.gather(
            *[self.get("/v1/history", page=p, perpage=50) for p in range(2, total + 1)]
        )
        for page_data in pages:
            records.extend(self.records(page_data))
        return records

    async def item(self, item_id: int) -> dict | None:
        data = await self.get_optional(f"/v1/items/{item_id}")
        return data["item"] if data else None

    async def watching(self, item_id: int) -> dict | None:
        data = await self.get_optional("/v1/watching", id=item_id)
        return data["item"] if data else None

    async def unwatched_movies(self) -> list:
        return self.records(await self.get("/v1/watching/movies"))

    async def watchlist(self) -> list:
        return self.records(await self.get("/v1/watching/serials", subscribed=1))
