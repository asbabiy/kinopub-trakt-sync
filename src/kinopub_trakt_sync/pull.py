"""Dump everything watch-related from kino.pub into data/kinopub_dump.json."""

import time

from . import config
from .kinopub import KinopubClient


def pull() -> dict:
    client = KinopubClient()

    history: list = []
    for page_data in client.history_pages():
        history.extend(client.records(page_data))
        print(f"history: {len(history)} records", flush=True)

    ids = {
        r["item"]["id"]
        for r in history
        if isinstance(r.get("item"), dict) and "id" in r["item"]
    }
    for record in client.unwatched_movies():
        if record.get("id"):
            ids.add(record["id"])
    watchlist = client.watchlist()
    for record in watchlist:
        if record.get("id"):
            ids.add(record["id"])

    items: dict = {}
    watching: dict = {}
    for n, item_id in enumerate(sorted(ids), 1):
        items[str(item_id)] = client.item(item_id)
        watching[str(item_id)] = client.watching(item_id)
        if n % 20 == 0 or n == len(ids):
            print(f"items: {n}/{len(ids)}", flush=True)
        time.sleep(0.2)

    dump = {
        "pulled_at": int(time.time()),
        "history": history,
        "items": items,
        "watching": watching,
        "watchlist": watchlist,
    }
    config.save_json(config.DUMP_FILE, dump)
    print(f"dump saved: {config.DUMP_FILE} ({len(ids)} items, {len(history)} history records)")
    return dump
