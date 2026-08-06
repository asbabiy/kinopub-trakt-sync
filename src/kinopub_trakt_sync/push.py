"""Apply a sync plan to Trakt.

Idempotency: Trakt does NOT dedupe history plays, so every pushed entry is
recorded in data/push_state.json and skipped on re-runs. Additionally,
items already watched on Trakt (fetched via /sync/watched) are skipped, so
running against a non-empty Trakt account will not double existing plays.
"""

import time

from . import config
from .trakt import TraktClient

HISTORY_CHUNK = 500


def episode_key(entry: dict) -> str:
    return f"episode:{entry['imdb']}:{entry['season']}:{entry['episode']}"


def _load_state() -> dict:
    if config.STATE_FILE.exists():
        return config.load_json(config.STATE_FILE)
    return {"pushed": [], "not_found": []}


def _watched_on_trakt(client: TraktClient) -> tuple[set, set]:
    movies = set()
    for entry in client.watched("movies"):
        imdb = ((entry.get("movie") or {}).get("ids") or {}).get("imdb")
        if imdb:
            movies.add(imdb)
    episodes = set()
    for entry in client.watched("shows"):
        imdb = ((entry.get("show") or {}).get("ids") or {}).get("imdb")
        if not imdb:
            continue
        for season in entry.get("seasons") or []:
            for ep in season.get("episodes") or []:
                episodes.add(f"episode:{imdb}:{season.get('number')}:{ep.get('number')}")
    return movies, episodes


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _shows_payload(episodes: list) -> list:
    shows: dict = {}
    for entry in episodes:
        seasons = shows.setdefault(entry["imdb"], {})
        seasons.setdefault(entry["season"], []).append(
            {"number": entry["episode"], "watched_at": entry["watched_at"]}
        )
    return [
        {
            "ids": {"imdb": imdb},
            "seasons": [
                {"number": number, "episodes": eps} for number, eps in sorted(seasons.items())
            ],
        }
        for imdb, seasons in shows.items()
    ]


def push_history(plan: dict, client: TraktClient, dry_run: bool) -> None:
    state = _load_state()
    pushed = set(state["pushed"])
    watched_movies, watched_episodes = _watched_on_trakt(client)

    movies = [
        m
        for m in plan["movies_watched"]
        if f"movie:{m['imdb']}" not in pushed and m["imdb"] not in watched_movies
    ]
    episodes = [
        e
        for e in plan["episodes_watched"]
        if episode_key(e) not in pushed and episode_key(e) not in watched_episodes
    ]
    skipped = (len(plan["movies_watched"]) - len(movies)) + (
        len(plan["episodes_watched"]) - len(episodes)
    )
    print(f"history: {len(movies)} movies + {len(episodes)} episodes to push, {skipped} already synced")
    if dry_run or (not movies and not episodes):
        return

    for movie_chunk in _chunks(movies, HISTORY_CHUNK):
        body = {
            "movies": [
                {"watched_at": m["watched_at"], "ids": {"imdb": m["imdb"]}} for m in movie_chunk
            ]
        }
        result = client.sync_history(body)
        state["pushed"].extend(f"movie:{m['imdb']}" for m in movie_chunk)
        state["not_found"].extend(result.get("not_found", {}).get("movies", []))
        config.save_json(config.STATE_FILE, state)
        print(f"  movies chunk: added {result.get('added', {}).get('movies', 0)}")
        time.sleep(1)

    for episode_chunk in _chunks(episodes, HISTORY_CHUNK):
        body = {"shows": _shows_payload(episode_chunk)}
        result = client.sync_history(body)
        state["pushed"].extend(episode_key(e) for e in episode_chunk)
        state["not_found"].extend(result.get("not_found", {}).get("shows", []))
        config.save_json(config.STATE_FILE, state)
        print(f"  episodes chunk: added {result.get('added', {}).get('episodes', 0)}")
        time.sleep(1)

    if state["not_found"]:
        print(f"not found on Trakt: {len(state['not_found'])} (see push_state.json)")


def push_progress(plan: dict, client: TraktClient, dry_run: bool) -> None:
    state = _load_state()
    pushed = set(state["pushed"])
    entries = [
        e for e in plan["progress"] if f"progress:{_progress_key(e)}" not in pushed
    ]
    skipped = len(plan["progress"]) - len(entries)
    print(f"progress: {len(entries)} items to push, {skipped} already synced")
    if dry_run:
        return

    for entry in entries:
        if entry["media"] == "movie":
            body = {"movie": {"ids": {"imdb": entry["imdb"]}}, "progress": entry["percent"]}
        else:
            body = {
                "show": {"ids": {"imdb": entry["imdb"]}},
                "episode": {"season": entry["season"], "number": entry["episode"]},
                "progress": entry["percent"],
            }
        outcome = client.scrobble_pause(body)
        if outcome != "rejected":
            state["pushed"].append(f"progress:{_progress_key(entry)}")
            config.save_json(config.STATE_FILE, state)
        print(f"  {entry['title']} -> {entry['percent']}% ({outcome})")
        time.sleep(1)


def _progress_key(entry: dict) -> str:
    if entry["media"] == "movie":
        return f"movie:{entry['imdb']}"
    return f"{entry['imdb']}:{entry['season']}:{entry['episode']}"


def push_watchlist(plan: dict, client: TraktClient, dry_run: bool) -> None:
    state = _load_state()
    pushed = set(state["pushed"])
    entries = [w for w in plan["watchlist"] if f"watchlist:{w['imdb']}" not in pushed]
    skipped = len(plan["watchlist"]) - len(entries)
    print(f"watchlist: {len(entries)} shows to push, {skipped} already synced")
    if dry_run or not entries:
        return

    body = {"shows": [{"ids": {"imdb": w["imdb"]}} for w in entries]}
    result = client.sync_watchlist(body)
    state["pushed"].extend(f"watchlist:{w['imdb']}" for w in entries)
    config.save_json(config.STATE_FILE, state)
    print(f"  added {result.get('added', {}).get('shows', 0)} shows to watchlist")
