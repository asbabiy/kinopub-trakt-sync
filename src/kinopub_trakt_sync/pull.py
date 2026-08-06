"""Dump everything watch-related from kino.pub into data/kinopub_dump.json.

Metadata (including the imdb id) comes from the item embedded in each history
record, so watched titles need no extra /v1/items call and survive removal from
the catalog. /v1/watching supplies per-episode status, position and timestamps
and answers even for deleted items. /v1/items is needed only for ids that never
appear in history — started-but-unwatched movies and the watchlist — and is
404-tolerant.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from rich.progress import Progress

from .kinopub import KinopubClient
from .settings import Settings
from .storage import write_json

log = logging.getLogger(__name__)


async def pull(settings: Settings) -> dict[str, Any]:
    async with KinopubClient(settings) as client:
        history = await client.history()
        print(f"history: {len(history)} records")

        # Item metadata rides along with each history record.
        items: dict[str, Any] = {
            str(record["item"]["id"]): record["item"]
            for record in history
            if isinstance(record.get("item"), dict) and record["item"].get("id")
        }

        # Ids that may be absent from history yet still carry watch state.
        unwatched, watchlist = await asyncio.gather(client.unwatched_movies(), client.watchlist())
        extra_ids = {record["id"] for record in (*unwatched, *watchlist) if record.get("id")}
        item_ids = sorted({int(key) for key in items} | extra_ids)

        async def fetch(item_id: int) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
            state = await client.watching(item_id)
            metadata = None if str(item_id) in items else await client.item(item_id)
            return item_id, state, metadata

        watching: dict[str, Any] = {}
        missing: list[int] = []
        with Progress(transient=True) as progress:
            task = progress.add_task("watch states", total=len(item_ids))
            for coroutine in asyncio.as_completed([fetch(item_id) for item_id in item_ids]):
                item_id, state, metadata = await coroutine
                if state is not None:
                    watching[str(item_id)] = state
                if metadata is not None:
                    items[str(item_id)] = metadata
                elif str(item_id) not in items:
                    missing.append(item_id)
                progress.advance(task)

    dump = {
        "pulled_at": int(time.time()),
        "history": history,
        "items": items,
        "watching": watching,
        "watchlist": watchlist,
    }
    write_json(settings.paths.dump, dump)
    if missing:
        print(f"skipped {len(missing)} ids with no metadata (deleted, absent from history)")
    print(
        f"dump saved: {settings.paths.dump} "
        f"({len(items)} items, {len(watching)} watch states, {len(history)} history records)"
    )
    return dump
