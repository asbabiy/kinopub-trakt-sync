"""Convert a kino.pub dump into a Trakt sync plan.

Matching is by IMDb id (kino.pub stores it as a bare integer). Items without
an IMDb id land in the "unmatched" bucket for manual review — Trakt cannot
reliably match them otherwise.
"""

from datetime import datetime, timezone

MOVIE_TYPES = {"movie", "documovie", "3d", "concert"}

# kino.pub episode/video status: -1 unwatched, 0 in progress, 1 watched.
WATCHED = 1


def imdb_id(item: dict) -> str | None:
    raw = item.get("imdb")
    if not raw:
        return None
    digits = str(raw).removeprefix("tt")
    if not digits.isdigit() or int(digits) == 0:
        return None
    return f"tt{int(digits):07d}"


def iso(ts) -> str:
    # Trakt accepts the literal "unknown" to mark a watch without a date.
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _percent(time_watched: int, duration: int) -> float | None:
    if not duration or not time_watched:
        return None
    percent = round(min(time_watched / duration * 100, 99.0), 1)
    # Trakt rejects scrobbles below 1%.
    return percent if percent >= 1 else None


def _plan_movie(plan: dict, base: dict, watching: dict) -> None:
    videos = watching.get("videos") or []
    if videos and all(v.get("status") == WATCHED for v in videos):
        watched_at = iso(max(v.get("updated") or 0 for v in videos))
        plan["movies_watched"].append({**base, "watched_at": watched_at})
        return
    percent = _percent(
        sum(v.get("time") or 0 for v in videos),
        sum(v.get("duration") or 0 for v in videos),
    )
    if percent is not None:
        plan["progress"].append({**base, "media": "movie", "percent": percent})


def _plan_show(plan: dict, base: dict, watching: dict) -> None:
    for season in watching.get("seasons") or []:
        for ep in season.get("episodes") or []:
            entry = {**base, "season": season.get("number"), "episode": ep.get("number")}
            if ep.get("status") == WATCHED:
                plan["episodes_watched"].append({**entry, "watched_at": iso(ep.get("updated"))})
                continue
            percent = _percent(ep.get("time") or 0, ep.get("duration") or 0)
            if percent is not None:
                plan["progress"].append({**entry, "media": "episode", "percent": percent})


def build_plan(dump: dict) -> dict:
    plan: dict = {
        "movies_watched": [],
        "episodes_watched": [],
        "progress": [],
        "watchlist": [],
        "unmatched": [],
    }

    for item_id, item in dump.get("items", {}).items():
        base = {
            "kinopub_id": int(item_id),
            "title": item.get("title"),
            "year": item.get("year"),
            "type": item.get("type"),
        }
        imdb = imdb_id(item)
        if not imdb:
            plan["unmatched"].append({**base, "reason": "no imdb id"})
            continue
        base["imdb"] = imdb
        watching = dump.get("watching", {}).get(item_id) or {}
        if item.get("type") in MOVIE_TYPES:
            _plan_movie(plan, base, watching)
        else:
            _plan_show(plan, base, watching)

    for record in dump.get("watchlist") or []:
        item = dump.get("items", {}).get(str(record.get("id"))) or record
        entry = {"kinopub_id": record.get("id"), "title": item.get("title"), "year": item.get("year")}
        imdb = imdb_id(item)
        if imdb:
            plan["watchlist"].append({**entry, "imdb": imdb})
        else:
            plan["unmatched"].append({**entry, "reason": "watchlist: no imdb id"})

    plan["stats"] = {
        "movies_watched": len(plan["movies_watched"]),
        "episodes_watched": len(plan["episodes_watched"]),
        "progress": len(plan["progress"]),
        "watchlist": len(plan["watchlist"]),
        "unmatched": len(plan["unmatched"]),
    }
    return plan


def print_summary(plan: dict) -> None:
    stats = plan["stats"]
    shows = {e["imdb"] for e in plan["episodes_watched"]}
    print(f"movies watched:   {stats['movies_watched']}")
    print(f"episodes watched: {stats['episodes_watched']} across {len(shows)} shows")
    print(f"in progress:      {stats['progress']}")
    print(f"watchlist:        {stats['watchlist']}")
    print(f"unmatched:        {stats['unmatched']}")
    for entry in plan["unmatched"][:15]:
        print(f"  - {entry.get('title')} ({entry.get('year')}): {entry.get('reason')}")
    if stats["unmatched"] > 15:
        print(f"  ... and {stats['unmatched'] - 15} more (see sync_plan.json)")
