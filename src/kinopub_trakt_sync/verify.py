"""Element-wise verification of the Trakt account against the sync plan.

Compares every planned movie/episode play (identity AND watched_at timestamp)
and every playback-progress entry with what the account actually holds, then
reports missing / extra / timestamp-mismatched items. With fix=True it removes
wrong history events (they are this tool's own artifacts — the source dump
stays untouched) and pushes the correct ones.
"""

import asyncio

from . import config
from .push import episode_key, history_payload, show_ids, target as _target
from .trakt import TraktClient


def _same_watched_at(expected: str, actual: str) -> bool:
    # Trakt stores watched_at with minute precision (seconds are zeroed),
    # so timestamps are compared truncated to the minute.
    return expected == "unknown" or expected[:16] == (actual or "")[:16]


async def _actual_history(client: TraktClient):
    """Index history events by every id the row exposes (imdb and trakt)."""
    movies: dict = {}
    for row in await client.paginated("/sync/history/movies"):
        ids = row["movie"]["ids"]
        event = {"id": row["id"], "watched_at": row["watched_at"]}
        for ref in (ids.get("imdb"), ids.get("trakt")):
            if ref:
                movies.setdefault(ref, []).append(event)
    episodes: dict = {}
    for row in await client.paginated("/sync/history/episodes"):
        ids = row["show"]["ids"]
        ep = row["episode"]
        event = {"id": row["id"], "watched_at": row["watched_at"]}
        for ref in (ids.get("imdb"), ids.get("trakt")):
            if ref:
                episodes.setdefault((ref, ep["season"], ep["number"]), []).append(event)
    return movies, episodes


async def _actual_playback(client: TraktClient) -> dict:
    playback = {}
    for row in await client.get_json("/sync/playback") or []:
        if row["type"] == "movie":
            for ref in (row["movie"]["ids"].get("imdb"), row["movie"]["ids"].get("trakt")):
                if ref:
                    playback[ref] = row["progress"]
        else:
            ids = row["show"]["ids"]
            ep = row["episode"]
            for ref in (ids.get("imdb"), ids.get("trakt")):
                if ref:
                    playback[(ref, ep["season"], ep["number"])] = row["progress"]
    return playback


async def build_report(plan: dict, client: TraktClient) -> dict:
    movies_actual, episodes_actual = await _actual_history(client)
    playback = await _actual_playback(client)

    report = {
        "missing_movies": [],
        "missing_episodes": [],
        "ts_mismatch": [],
        "extra_events": [],
        "progress_missing": [],
        "progress_mismatch": [],
        "ok": {"movies": 0, "episodes": 0, "progress": 0},
    }

    claimed_event_ids = set()
    for m in plan["movies_watched"]:
        events = movies_actual.get(m["imdb"], [])
        if not events:
            report["missing_movies"].append(m)
            continue
        event = events[0]
        claimed_event_ids.add(event["id"])
        if not _same_watched_at(m["watched_at"], event["watched_at"]):
            report["ts_mismatch"].append(
                {**m, "actual": event["watched_at"], "event_id": event["id"]}
            )
        else:
            report["ok"]["movies"] += 1

    for e in plan["episodes_watched"]:
        events = episodes_actual.get(_target(e), [])
        if not events:
            report["missing_episodes"].append(e)
            continue
        event = events[0]
        claimed_event_ids.add(event["id"])
        if not _same_watched_at(e["watched_at"], event["watched_at"]):
            report["ts_mismatch"].append(
                {**e, "actual": event["watched_at"], "event_id": event["id"]}
            )
        else:
            report["ok"]["episodes"] += 1

    # Any history event not claimed by the plan is an artifact of a bad push
    # (e.g. episodes recorded under shifted identities before reconciliation).
    seen = set()
    for key, events in list(movies_actual.items()) + list(episodes_actual.items()):
        for event in events:
            if event["id"] not in claimed_event_ids and event["id"] not in seen:
                seen.add(event["id"])
                report["extra_events"].append({"key": str(key), "event_id": event["id"]})

    for p in plan["progress"]:
        key = _target(p) if p["media"] == "episode" else p["imdb"]
        actual = playback.get(key)
        if actual is None:
            report["progress_missing"].append(p)
        elif abs(actual - p["percent"]) > 0.1:
            report["progress_mismatch"].append({**p, "actual": actual})
        else:
            report["ok"]["progress"] += 1

    return report


async def apply_fixes(plan: dict, report: dict, client: TraktClient) -> None:
    remove_ids = [x["event_id"] for x in report["extra_events"]] + [
        x["event_id"] for x in report["ts_mismatch"]
    ]
    if remove_ids:
        deleted = (await client.history_remove(remove_ids)).get("deleted", {})
        print(f"removed wrong events: {deleted}")
        await asyncio.sleep(1)

    movies = report["missing_movies"] + [
        x for x in report["ts_mismatch"] if "season" not in x
    ]
    episodes = report["missing_episodes"] + [
        x for x in report["ts_mismatch"] if "season" in x
    ]
    if movies or episodes:
        result = await client.sync_history(history_payload(movies, episodes))
        print(f"pushed: {result.get('added')}  not_found: {result.get('not_found')}")
        await asyncio.sleep(1)

    for p in report["progress_missing"] + report["progress_mismatch"]:
        if p["media"] == "movie":
            body = {"movie": {"ids": {"imdb": p["imdb"]}}, "progress": p["percent"]}
        else:
            show, season, number = _target(p)
            body = {
                "show": {"ids": show_ids(show)},
                "episode": {"season": season, "number": number},
                "progress": p["percent"],
            }
        outcome = await client.scrobble_pause(body)
        print(f"  progress {p['title']} -> {p['percent']}% ({outcome})")
        await asyncio.sleep(1)

    # The account now reflects the plan: make the push state match it.
    state = {"pushed": [], "not_found": []}
    if config.STATE_FILE.exists():
        state["not_found"] = config.load_json(config.STATE_FILE).get("not_found", [])
    state["pushed"] = (
        [f"movie:{m['imdb']}" for m in plan["movies_watched"]]
        + [episode_key(e) for e in plan["episodes_watched"]]
        + [
            f"progress:{p['imdb'] if p['media'] == 'movie' else ':'.join(map(str, (p['imdb'], p['season'], p['episode'])))}"
            for p in plan["progress"]
        ]
        + [f"watchlist:{w['imdb']}" for w in plan["watchlist"]]
    )
    config.save_json(config.STATE_FILE, state)


def print_report(report: dict) -> None:
    ok = report["ok"]
    print(f"verified ok: {ok['movies']} movies, {ok['episodes']} episodes, {ok['progress']} progress")
    for key in (
        "missing_movies",
        "missing_episodes",
        "ts_mismatch",
        "extra_events",
        "progress_missing",
        "progress_mismatch",
    ):
        rows = report[key]
        if not rows:
            continue
        print(f"{key}: {len(rows)}")
        for row in rows[:10]:
            label = row.get("title") or row.get("key")
            extra = ""
            if "season" in row:
                extra = f" s{row['season']}e{row['episode']}"
                if row.get("target"):
                    t = row["target"]
                    extra += f" -> {t['show']} s{t['season']}e{t['episode']}"
            if "actual" in row:
                extra += f" (expected {row.get('watched_at') or row.get('percent')}, actual {row['actual']})"
            print(f"  - {label}{extra}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
