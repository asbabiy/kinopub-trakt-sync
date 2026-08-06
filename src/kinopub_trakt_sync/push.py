"""Apply a sync plan to Trakt.

Idempotency has two layers, because Trakt does not deduplicate history plays:
locally, every pushed entry is recorded in data/push_state.json and skipped on
re-runs; remotely, items already watched on the account are skipped too, so a
first run against a non-empty account does not double existing plays.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import EpisodeWatch, MovieWatch, Plan, Progress, ShowRef, Target, WatchlistShow
from .storage import read_model, write_model
from .trakt import TraktClient

log = logging.getLogger(__name__)

HISTORY_CHUNK = 500
WRITE_INTERVAL_SECONDS = 1  # Trakt hard-limits writes to 1 per second


class PushState(BaseModel):
    """Which plan entries already reached Trakt.

    Sole owner of the key format: everything else asks an entry for its
    `state_key`, so push and verify can never drift apart.
    """

    pushed: list[str] = []
    not_found: list[dict[str, Any]] = []

    @classmethod
    def load(cls, path: Path) -> PushState:
        return read_model(path, cls) or cls()

    def save(self, path: Path) -> None:
        write_model(path, self)

    def __contains__(self, entry: MovieWatch | EpisodeWatch | Progress | WatchlistShow) -> bool:
        return entry.state_key in set(self.pushed)

    def record(self, entries: Iterable[MovieWatch | EpisodeWatch | Progress | WatchlistShow]) -> None:
        self.pushed.extend(entry.state_key for entry in entries)


def history_payload(movies: list[MovieWatch], episodes: list[EpisodeWatch]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if movies:
        body["movies"] = [{"watched_at": movie.watched_at, "ids": {"imdb": movie.imdb}} for movie in movies]
    if episodes:
        shows: dict[ShowRef, dict[int, list[dict[str, Any]]]] = {}
        for entry in episodes:
            target = entry.trakt
            seasons = shows.setdefault(target.show, {})
            seasons.setdefault(target.season, []).append(
                {"number": target.episode, "watched_at": entry.watched_at}
            )
        body["shows"] = [
            {
                "ids": Target(show=show, season=0, episode=0).ids,
                "seasons": [
                    {"number": number, "episodes": episodes} for number, episodes in sorted(seasons.items())
                ],
            }
            for show, seasons in shows.items()
        ]
    return body


def progress_payload(entry: Progress) -> dict[str, Any]:
    if (target := entry.trakt) is None:
        return {"movie": {"ids": {"imdb": entry.imdb}}, "progress": entry.percent}
    return {
        "show": {"ids": target.ids},
        "episode": {"season": target.season, "number": target.episode},
        "progress": entry.percent,
    }


async def watched_on_trakt(client: TraktClient) -> tuple[set[ShowRef], set[tuple[ShowRef, int, int]]]:
    """Everything the account already counts as watched, indexed by every id
    Trakt exposes, so imdb-addressed and trakt-addressed entries both match."""
    # Only the ids we address items by: Trakt also nests objects here (`plex`
    # is a dict), so taking every value would be both wrong and unhashable.
    movies: set[ShowRef] = set()
    for row in await client.watched("movies"):
        ids = (row.get("movie") or {}).get("ids") or {}
        movies.update(ref for ref in (ids.get("imdb"), ids.get("trakt")) if ref)

    episodes: set[tuple[ShowRef, int, int]] = set()
    for row in await client.watched("shows"):
        ids = (row.get("show") or {}).get("ids") or {}
        refs = [ref for ref in (ids.get("imdb"), ids.get("trakt")) if ref]
        for season in row.get("seasons") or []:
            for episode in season.get("episodes") or []:
                episodes.update((ref, season["number"], episode["number"]) for ref in refs)
    return movies, episodes


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def push_history(
    plan: Plan, client: TraktClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    watched_movies, watched_episodes = await watched_on_trakt(client)
    movies = [m for m in plan.movies if m not in state and m.imdb not in watched_movies]
    episodes = [e for e in plan.episodes if e not in state and e.trakt.key not in watched_episodes]
    skipped = (len(plan.movies) - len(movies)) + (len(plan.episodes) - len(episodes))
    print(f"history: {len(movies)} movies + {len(episodes)} episodes to push, {skipped} already synced")
    if dry_run:
        return

    for chunk in _chunks(movies, HISTORY_CHUNK):
        result = await client.add_to_history(history_payload(chunk, []))
        state.record(chunk)
        state.not_found.extend(result.get("not_found", {}).get("movies", []))
        state.save(state_path)
        print(f"  movies chunk: added {result.get('added', {}).get('movies', 0)}")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    for chunk in _chunks(episodes, HISTORY_CHUNK):
        result = await client.add_to_history(history_payload([], chunk))
        state.record(chunk)
        state.not_found.extend(result.get("not_found", {}).get("shows", []))
        state.save(state_path)
        print(f"  episodes chunk: added {result.get('added', {}).get('episodes', 0)}")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    if state.not_found:
        print(f"not found on Trakt: {len(state.not_found)} (see push_state.json)")


async def push_progress(
    plan: Plan, client: TraktClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    entries = [entry for entry in plan.progress if entry not in state]
    print(f"progress: {len(entries)} items to push, {len(plan.progress) - len(entries)} already synced")
    if dry_run:
        return

    for entry in entries:
        outcome = await client.scrobble_pause(progress_payload(entry))
        # A rejected pause stored nothing, so it must stay pushable.
        if outcome != "rejected":
            state.record([entry])
            state.save(state_path)
        print(f"  {entry.title} -> {entry.percent}% ({outcome})")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)


async def push_watchlist(
    plan: Plan, client: TraktClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    entries = [show for show in plan.watchlist if show not in state]
    print(f"watchlist: {len(entries)} shows to push, {len(plan.watchlist) - len(entries)} already synced")
    if dry_run or not entries:
        return

    result = await client.add_to_watchlist({"shows": [{"ids": {"imdb": show.imdb}} for show in entries]})
    state.record(entries)
    state.save(state_path)
    print(f"  added {result.get('added', {}).get('shows', 0)} shows to watchlist")
