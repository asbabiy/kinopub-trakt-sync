"""Apply a sync plan to Trakt.

Idempotency: Trakt does NOT dedupe history plays, so every pushed entry is
recorded in data/push_state.json and skipped on re-runs. Additionally,
items already watched on Trakt (fetched via /sync/watched) are skipped, so
running against a non-empty Trakt account will not double existing plays.
"""

import asyncio

from . import config
from .trakt import TraktClient

HISTORY_CHUNK = 500


def episode_key(entry: dict) -> str:
    # Keyed by kino.pub identity: stable across re-plans even if the Trakt
    # target of an entry is later remapped.
    return f"episode:{entry['imdb']}:{entry['season']}:{entry['episode']}"


def target(entry: dict) -> tuple:
    """(show ref, season, episode) on Trakt — remapped when reconcile did."""
    t = entry.get("target")
    if t:
        return (t["show"], t["season"], t["episode"])
    return (entry["imdb"], entry["season"], entry["episode"])


def show_ids(ref) -> dict:
    return {"trakt": ref} if isinstance(ref, int) else {"imdb": ref}


def history_payload(movies: list, episodes: list) -> dict:
    body: dict = {}
    if movies:
        body["movies"] = [
            {"watched_at": m["watched_at"], "ids": {"imdb": m["imdb"]}} for m in movies
        ]
    if episodes:
        shows: dict = {}
        for e in episodes:
            show, season, number = target(e)
            seasons = shows.setdefault(show, {})
            seasons.setdefault(season, []).append(
                {"number": number, "watched_at": e["watched_at"]}
            )
        body["shows"] = [
            {
                "ids": show_ids(show),
                "seasons": [{"number": n, "episodes": eps} for n, eps in sorted(seasons.items())],
            }
            for show, seasons in shows.items()
        ]
    return body


def _load_state() -> dict:
    if config.STATE_FILE.exists():
        return config.load_json(config.STATE_FILE)
    return {"pushed": [], "not_found": []}


async def _watched_on_trakt(client: TraktClient) -> tuple[set, set]:
    movies = set()
    for entry in await client.watched("movies"):
        for ref in ((entry.get("movie") or {}).get("ids") or {}).values():
            movies.add(ref)
    episodes = set()
    for entry in await client.watched("shows"):
        ids = (entry.get("show") or {}).get("ids") or {}
        for season in entry.get("seasons") or []:
            for ep in season.get("episodes") or []:
                for ref in (ids.get("imdb"), ids.get("trakt")):
                    if ref:
                        episodes.add((ref, season.get("number"), ep.get("number")))
    return movies, episodes


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def push_history(plan: dict, client: TraktClient, dry_run: bool) -> None:
    state = _load_state()
    pushed = set(state["pushed"])
    watched_movies, watched_episodes = await _watched_on_trakt(client)

    movies = [
        m
        for m in plan["movies_watched"]
        if f"movie:{m['imdb']}" not in pushed and m["imdb"] not in watched_movies
    ]
    episodes = [
        e
        for e in plan["episodes_watched"]
        if episode_key(e) not in pushed and target(e) not in watched_episodes
    ]
    skipped = (len(plan["movies_watched"]) - len(movies)) + (
        len(plan["episodes_watched"]) - len(episodes)
    )
    print(f"history: {len(movies)} movies + {len(episodes)} episodes to push, {skipped} already synced")
    if dry_run or (not movies and not episodes):
        return

    for movie_chunk in _chunks(movies, HISTORY_CHUNK):
        result = await client.sync_history(history_payload(movie_chunk, []))
        state["pushed"].extend(f"movie:{m['imdb']}" for m in movie_chunk)
        state["not_found"].extend(result.get("not_found", {}).get("movies", []))
        config.save_json(config.STATE_FILE, state)
        print(f"  movies chunk: added {result.get('added', {}).get('movies', 0)}")
        await asyncio.sleep(1)

    for episode_chunk in _chunks(episodes, HISTORY_CHUNK):
        result = await client.sync_history(history_payload([], episode_chunk))
        state["pushed"].extend(episode_key(e) for e in episode_chunk)
        state["not_found"].extend(result.get("not_found", {}).get("shows", []))
        config.save_json(config.STATE_FILE, state)
        print(f"  episodes chunk: added {result.get('added', {}).get('episodes', 0)}")
        await asyncio.sleep(1)

    if state["not_found"]:
        print(f"not found on Trakt: {len(state['not_found'])} (see push_state.json)")


async def push_progress(plan: dict, client: TraktClient, dry_run: bool) -> None:
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
            show, season, number = target(entry)
            body = {
                "show": {"ids": show_ids(show)},
                "episode": {"season": season, "number": number},
                "progress": entry["percent"],
            }
        outcome = await client.scrobble_pause(body)
        if outcome != "rejected":
            state["pushed"].append(f"progress:{_progress_key(entry)}")
            config.save_json(config.STATE_FILE, state)
        print(f"  {entry['title']} -> {entry['percent']}% ({outcome})")
        await asyncio.sleep(1)


def _progress_key(entry: dict) -> str:
    if entry["media"] == "movie":
        return f"movie:{entry['imdb']}"
    return f"{entry['imdb']}:{entry['season']}:{entry['episode']}"


async def push_watchlist(plan: dict, client: TraktClient, dry_run: bool) -> None:
    state = _load_state()
    pushed = set(state["pushed"])
    entries = [w for w in plan["watchlist"] if f"watchlist:{w['imdb']}" not in pushed]
    skipped = len(plan["watchlist"]) - len(entries)
    print(f"watchlist: {len(entries)} shows to push, {skipped} already synced")
    if dry_run or not entries:
        return

    body = {"shows": [{"ids": {"imdb": w["imdb"]}} for w in entries]}
    result = await client.sync_watchlist(body)
    state["pushed"].extend(f"watchlist:{w['imdb']}" for w in entries)
    config.save_json(config.STATE_FILE, state)
    print(f"  added {result.get('added', {}).get('shows', 0)} shows to watchlist")
