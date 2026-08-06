"""kino.pub API client: device-code auth and read-only data extraction.

API reference: https://kinoapi.com/ (base https://api.service-kp.com/v1).
"""

import time

import httpx

from . import config


class KinopubError(RuntimeError):
    pass


class KinopubClient:
    def __init__(self) -> None:
        self._http = httpx.Client(timeout=30)

    # -- auth ------------------------------------------------------------

    def device_auth(self) -> None:
        resp = self._http.post(
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
            time.sleep(data.get("interval", 5))
            resp = self._http.post(
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

    def _access_token(self) -> str:
        tokens = config.load_tokens().get("kinopub")
        if not tokens:
            raise KinopubError("not authorized, run: kts auth kinopub")
        if time.time() > tokens["obtained_at"] + tokens.get("expires_in", 3600) - 60:
            resp = self._http.post(
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

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self._access_token()
        resp = self._http.get(f"{config.KINOPUB_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_optional(self, path: str, **params):
        """Like get(), but returns None on 404 (item deleted from catalog)."""
        try:
            return self.get(path, **params)
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

    def history_pages(self):
        page = 1
        while True:
            data = self.get("/v1/history", page=page, perpage=50)
            yield data
            pagination = data.get("pagination") or {}
            if page >= int(pagination.get("total") or 0):
                return
            page += 1

    def item(self, item_id: int) -> dict | None:
        data = self.get_optional(f"/v1/items/{item_id}")
        return data["item"] if data else None

    def watching(self, item_id: int) -> dict | None:
        data = self.get_optional("/v1/watching", id=item_id)
        return data["item"] if data else None

    def unwatched_movies(self) -> list:
        return self.records(self.get("/v1/watching/movies"))

    def watchlist(self) -> list:
        return self.records(self.get("/v1/watching/serials", subscribed=1))
