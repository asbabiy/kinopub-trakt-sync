"""Dump everything watch-related from kino.pub into data/kinopub_dump.json.

Metadata (incl. IMDb id) comes from the item embedded in each history record,
so watched titles need no extra /v1/items call and survive being removed from
the catalog. /v1/watching gives per-episode status/progress/timestamps and
answers 200 even for deleted items. /v1/items is only needed for ids that never
appear in history (started-but-unwatched movies, watchlist) and is 404-tolerant.
"""

import time

from . import config
from .kinopub import KinopubClient

CHECKPOINT_EVERY = 25


def pull() -> dict:
    client = KinopubClient()

    history: list = []
    for page_data in client.history_pages():
        history.extend(client.records(page_data))
        print(f"history: {len(history)} records", flush=True)

    # Metadata from the item embedded in each history record (carries imdb).
    items: dict = {}
    for record in history:
        item = record.get("item")
        if isinstance(item, dict) and item.get("id"):
            items[str(item["id"])] = item

    # Ids that may not be in history and still need progress/metadata.
    extra_ids: set[int] = set()
    for record in client.unwatched_movies():
        if record.get("id"):
            extra_ids.add(record["id"])
    watchlist = client.watchlist()
    for record in watchlist:
        if record.get("id"):
            extra_ids.add(record["id"])

    all_ids = sorted({int(k) for k in items} | extra_ids)
    watching: dict = {}
    missing: list[int] = []

    dump = {
        "pulled_at": int(time.time()),
        "history": history,
        "items": items,
        "watching": watching,
        "watchlist": watchlist,
    }

    for n, item_id in enumerate(all_ids, 1):
        key = str(item_id)
        state = client.watching(item_id)
        if state is not None:
            watching[key] = state
        if key not in items:
            meta = client.item(item_id)
            if meta is not None:
                items[key] = meta
            else:
                missing.append(item_id)
        if n % CHECKPOINT_EVERY == 0 or n == len(all_ids):
            dump["pulled_at"] = int(time.time())
            config.save_json(config.DUMP_FILE, dump)
            print(f"items: {n}/{len(all_ids)} (checkpoint saved)", flush=True)
        time.sleep(0.2)

    if missing:
        print(f"skipped {len(missing)} ids with no metadata (deleted, not in history)")
    print(
        f"dump saved: {config.DUMP_FILE} "
        f"({len(items)} items, {len(watching)} watch states, {len(history)} history records)"
    )
    return dump
