"""Dump everything watch-related from kino.pub into data/kinopub_dump.json.

Metadata (incl. IMDb id) comes from the item embedded in each history record,
so watched titles need no extra /v1/items call and survive being removed from
the catalog. /v1/watching gives per-episode status/progress/timestamps and
answers 200 even for deleted items. /v1/items is only needed for ids that never
appear in history (started-but-unwatched movies, watchlist) and is 404-tolerant.

All per-item requests run concurrently (bounded by the client's semaphore).
"""

import asyncio
import time

from . import config
from .kinopub import KinopubClient


async def pull() -> dict:
    client = KinopubClient()

    history = await client.history_all()
    print(f"history: {len(history)} records", flush=True)

    # Metadata from the item embedded in each history record (carries imdb).
    items: dict = {}
    for record in history:
        item = record.get("item")
        if isinstance(item, dict) and item.get("id"):
            items[str(item["id"])] = item

    # Ids that may not be in history and still need progress/metadata.
    unwatched, watchlist = await asyncio.gather(client.unwatched_movies(), client.watchlist())
    extra_ids = {r["id"] for r in unwatched + watchlist if r.get("id")}

    all_ids = sorted({int(k) for k in items} | extra_ids)

    async def fetch(item_id: int):
        state = await client.watching(item_id)
        meta = None if str(item_id) in items else await client.item(item_id)
        return item_id, state, meta

    watching: dict = {}
    missing: list[int] = []
    for item_id, state, meta in await asyncio.gather(*[fetch(i) for i in all_ids]):
        if state is not None:
            watching[str(item_id)] = state
        if meta is not None:
            items[str(item_id)] = meta
        elif str(item_id) not in items:
            missing.append(item_id)

    dump = {
        "pulled_at": int(time.time()),
        "history": history,
        "items": items,
        "watching": watching,
        "watchlist": watchlist,
    }
    config.save_json(config.DUMP_FILE, dump)
    if missing:
        print(f"skipped {len(missing)} ids with no metadata (deleted, not in history)")
    print(
        f"dump saved: {config.DUMP_FILE} "
        f"({len(items)} items, {len(watching)} watch states, {len(history)} history records)"
    )
    return dump
