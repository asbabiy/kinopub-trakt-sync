"""Element-wise verification of the Trakt account against the sync plan.

Compares every planned play (identity and watched_at) and every playback
percent with what the account actually holds, then reports missing, extra and
mismatched entries. With `fix` it removes the wrong history events — they are
this tool's own artifacts, the kino.pub source is never touched — and pushes
the correct ones.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import EpisodeWatch, MovieWatch, Plan, Progress, ShowRef
from .push import PushState, history_payload, progress_payload
from .trakt import TraktClient

log = logging.getLogger(__name__)

PERCENT_TOLERANCE = 0.1
REPORT_PREVIEW = 10
WRITE_INTERVAL_SECONDS = 1


class HistoryEvent(BaseModel):
    id: int
    watched_at: str


class TimestampMismatch(BaseModel):
    title: str
    expected: str
    actual: str
    event_id: int
    season: int | None = None
    episode: int | None = None


class ProgressMismatch(BaseModel):
    title: str
    expected: float
    actual: float


class ExtraEvent(BaseModel):
    key: str
    event_id: int


class VerifyReport(BaseModel):
    missing_movies: list[MovieWatch] = []
    missing_episodes: list[EpisodeWatch] = []
    timestamp_mismatches: list[TimestampMismatch] = []
    extra_events: list[ExtraEvent] = []
    progress_missing: list[Progress] = []
    progress_mismatches: list[ProgressMismatch] = []
    verified_movies: int = 0
    verified_episodes: int = 0
    verified_progress: int = 0

    @property
    def problem_count(self) -> int:
        return (
            len(self.missing_movies)
            + len(self.missing_episodes)
            + len(self.timestamp_mismatches)
            + len(self.extra_events)
            + len(self.progress_missing)
            + len(self.progress_mismatches)
        )


def same_moment(expected: str, actual: str) -> bool:
    """Trakt stores watched_at with minute precision (seconds are zeroed), so
    timestamps are compared truncated to the minute."""
    return expected == "unknown" or expected[:16] == (actual or "")[:16]


async def _actual_history(
    client: TraktClient,
) -> tuple[dict[ShowRef, list[HistoryEvent]], dict[tuple[ShowRef, int, int], list[HistoryEvent]]]:
    movie_rows, episode_rows = await asyncio.gather(
        client.paginated("/sync/history/movies"), client.paginated("/sync/history/episodes")
    )

    movies: dict[ShowRef, list[HistoryEvent]] = {}
    for row in movie_rows:
        event = HistoryEvent.model_validate(row)
        ids = row["movie"]["ids"]
        for ref in (ids.get("imdb"), ids.get("trakt")):
            if ref:
                movies.setdefault(ref, []).append(event)

    episodes: dict[tuple[ShowRef, int, int], list[HistoryEvent]] = {}
    for row in episode_rows:
        event = HistoryEvent.model_validate(row)
        ids = row["show"]["ids"]
        episode = row["episode"]
        for ref in (ids.get("imdb"), ids.get("trakt")):
            if ref:
                key = (ref, episode["season"], episode["number"])
                episodes.setdefault(key, []).append(event)
    return movies, episodes


async def _actual_playback(client: TraktClient) -> dict[Any, float]:
    playback: dict[Any, float] = {}
    for row in await client.playback():
        if row["type"] == "movie":
            ids = row["movie"]["ids"]
            for ref in (ids.get("imdb"), ids.get("trakt")):
                if ref:
                    playback[ref] = row["progress"]
        else:
            ids = row["show"]["ids"]
            episode = row["episode"]
            for ref in (ids.get("imdb"), ids.get("trakt")):
                if ref:
                    playback[ref, episode["season"], episode["number"]] = row["progress"]
    return playback


def _check_history(
    plan: Plan,
    movies_actual: dict[ShowRef, list[HistoryEvent]],
    episodes_actual: dict[tuple[ShowRef, int, int], list[HistoryEvent]],
    report: VerifyReport,
) -> set[int]:
    """Match planned plays against the account; returns the claimed event ids."""
    claimed: set[int] = set()

    for movie in plan.movies:
        events = movies_actual.get(movie.imdb)
        if not events:
            report.missing_movies.append(movie)
            continue
        claimed.add(events[0].id)
        if same_moment(movie.watched_at, events[0].watched_at):
            report.verified_movies += 1
        else:
            report.timestamp_mismatches.append(
                TimestampMismatch(
                    title=movie.title,
                    expected=movie.watched_at,
                    actual=events[0].watched_at,
                    event_id=events[0].id,
                )
            )

    for episode in plan.episodes:
        events = episodes_actual.get(episode.trakt.key)
        if not events:
            report.missing_episodes.append(episode)
            continue
        claimed.add(events[0].id)
        if same_moment(episode.watched_at, events[0].watched_at):
            report.verified_episodes += 1
        else:
            report.timestamp_mismatches.append(
                TimestampMismatch(
                    title=episode.title,
                    expected=episode.watched_at,
                    actual=events[0].watched_at,
                    event_id=events[0].id,
                    season=episode.season,
                    episode=episode.episode,
                )
            )
    return claimed


def _check_progress(plan: Plan, playback: dict[Any, float], report: VerifyReport) -> None:
    for entry in plan.progress:
        target = entry.trakt
        actual = playback.get(target.key if target else entry.imdb)
        if actual is None:
            report.progress_missing.append(entry)
        elif abs(actual - entry.percent) > PERCENT_TOLERANCE:
            report.progress_mismatches.append(
                ProgressMismatch(title=entry.title, expected=entry.percent, actual=actual)
            )
        else:
            report.verified_progress += 1


async def build_report(plan: Plan, client: TraktClient) -> VerifyReport:
    (movies_actual, episodes_actual), playback = await asyncio.gather(
        _actual_history(client), _actual_playback(client)
    )
    report = VerifyReport()
    claimed = _check_history(plan, movies_actual, episodes_actual, report)

    # A history event that no plan entry claims is residue of an earlier bad
    # push — episodes recorded under shifted identities before reconciliation
    # existed, for instance.
    seen: set[int] = set()
    for key, events in (*movies_actual.items(), *episodes_actual.items()):
        for event in events:
            if event.id not in claimed and event.id not in seen:
                seen.add(event.id)
                report.extra_events.append(ExtraEvent(key=str(key), event_id=event.id))

    _check_progress(plan, playback, report)
    return report


def adopt_plan_as_state(plan: Plan, state_path: Path) -> None:
    """Record the whole plan as pushed.

    Called once the account has been shown to match the plan: every entry
    demonstrably reached Trakt, so the local state must say so. This also
    repairs a state file written by an older version with different keys —
    otherwise entries look unpushed forever and get sent again.
    """
    state = PushState()
    state.record([*plan.movies, *plan.episodes, *plan.progress, *plan.watchlist])
    state.save(state_path)


async def apply_fixes(plan: Plan, report: VerifyReport, client: TraktClient, state_path: Path) -> None:
    wrong_events = [event.event_id for event in report.extra_events] + [
        mismatch.event_id for mismatch in report.timestamp_mismatches
    ]
    if wrong_events:
        deleted = (await client.remove_from_history(wrong_events)).get("deleted", {})
        print(f"removed wrong events: {deleted}")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    mismatched = {(m.title, m.season, m.episode) for m in report.timestamp_mismatches}
    movies = report.missing_movies + [
        movie for movie in plan.movies if (movie.title, None, None) in mismatched
    ]
    episodes = report.missing_episodes + [
        episode for episode in plan.episodes if (episode.title, episode.season, episode.episode) in mismatched
    ]
    if movies or episodes:
        result = await client.add_to_history(history_payload(movies, episodes))
        print(f"pushed: {result.get('added')}  not_found: {result.get('not_found')}")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    stale = {mismatch.title for mismatch in report.progress_mismatches}
    for entry in report.progress_missing + [p for p in plan.progress if p.title in stale]:
        outcome = await client.scrobble_pause(progress_payload(entry))
        print(f"  progress {entry.title} -> {entry.percent}% ({outcome})")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    # The account now reflects the plan, so the local state must say the same.
    adopt_plan_as_state(plan, state_path)


def format_report(report: VerifyReport) -> str:
    lines = [
        f"verified ok: {report.verified_movies} movies, "
        f"{report.verified_episodes} episodes, {report.verified_progress} progress"
    ]
    sections: list[tuple[str, Sequence[ReportRow]]] = [
        ("missing movies", report.missing_movies),
        ("missing episodes", report.missing_episodes),
        ("timestamp mismatches", report.timestamp_mismatches),
        ("extra events", report.extra_events),
        ("progress missing", report.progress_missing),
        ("progress mismatches", report.progress_mismatches),
    ]
    for name, rows in sections:
        if not rows:
            continue
        lines.append(f"{name}: {len(rows)}")
        for row in rows[:REPORT_PREVIEW]:
            lines.append(f"  - {_describe(row)}")
        if len(rows) > REPORT_PREVIEW:
            lines.append(f"  ... and {len(rows) - REPORT_PREVIEW} more")
    return "\n".join(lines)


type ReportRow = MovieWatch | EpisodeWatch | Progress | TimestampMismatch | ProgressMismatch | ExtraEvent


def _describe(row: ReportRow) -> str:
    if isinstance(row, ExtraEvent):
        return f"{row.key} (event {row.event_id})"
    if isinstance(row, TimestampMismatch):
        where = f" s{row.season}e{row.episode}" if row.season is not None else ""
        return f"{row.title}{where}: expected {row.expected}, actual {row.actual}"
    if isinstance(row, ProgressMismatch):
        return f"{row.title}: expected {row.expected}%, actual {row.actual}%"
    if isinstance(row, EpisodeWatch):
        target = row.trakt
        return f"{row.title} s{row.season}e{row.episode} -> {target.show} s{target.season}e{target.episode}"
    if isinstance(row, Progress) and row.is_episode:
        return f"{row.title} s{row.season}e{row.episode} ({row.percent}%)"
    return f"{row.title}"
