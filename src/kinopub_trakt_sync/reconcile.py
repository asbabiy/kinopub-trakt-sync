"""Recover true Trakt episode identities behind kino.pub season numbering.

kino.pub numbers specials inline: a Christmas special can sit at position 1 and
shift the whole season, or belong to a different Trakt show entirely (era
reboots). Trakt keeps specials in season 0. Raw season/episode numbers
therefore mislabel episodes silently whenever the per-season counts diverge.

Division of labor:
- code fetches ground truth deterministically (Trakt season structures, ru
  translations, air dates, runtimes, candidate shows) and validates every
  proposed target against it;
- the model decides the matching across all metadata at once.

Seasons whose counts agree never reach the model. Anything the model cannot
place with certainty, or that fails validation, is reported as unresolved —
never guessed. Decisions are cached by season fingerprint, so repeat runs cost
nothing and stay deterministic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from .gemini import Gemini
from .models import (
    Dump,
    EpisodeWatch,
    KinopubItem,
    KinopubWatching,
    Plan,
    Progress,
    ShowRef,
    Target,
    Unmatched,
)
from .prompts import SEASON_MATCH, SHOW_MATCH
from .trakt import TraktClient

log = logging.getLogger(__name__)

CAST_PREVIEW = 200
PLOT_PREVIEW = 400


class TraktEpisode(BaseModel):
    number: int
    title: str | None = None
    first_aired: str | None = None
    runtime: int | None = None


class ShowCandidate(BaseModel):
    trakt: int
    imdb: str | None = None
    title: str | None = None
    year: int | None = None


class ShowAnswer(BaseModel):
    """Model reply for show identification."""

    trakt: int | None = Field(default=None, description="trakt id of the matching candidate")
    reason: str = ""


class EpisodeAnswerRow(BaseModel):
    kp: int = Field(description="kino.pub episode number")
    show: str | None = Field(default=None, description="candidate show, empty when unresolved")
    season: int | None = None
    episode: int | None = None
    unresolved: str | None = Field(default=None, description="reason, when identity is not certain")


class SeasonAnswer(BaseModel):
    mapping: list[EpisodeAnswerRow] = []


class SeasonMatch(BaseModel):
    """Validated outcome of reconciling one kino.pub season."""

    mapping: dict[int, Target] = {}
    unresolved: dict[int, str] = {}


class Catalog:
    """Cached read-only view of Trakt show structures.

    The cache survives runs on disk; concurrent lookups of one key share a
    single in-flight request.
    """

    def __init__(self, client: TraktClient, cache: dict[str, Any]) -> None:
        self._client = client
        self._cache = cache
        self._pending: dict[str, asyncio.Task[Any]] = {}

    async def _get(self, path: str, **params: Any) -> Any:
        query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        cache_key = f"{path}?{query}" if query else path
        if cache_key in self._cache:
            return self._cache[cache_key]
        if cache_key not in self._pending:
            self._pending[cache_key] = asyncio.create_task(self._client.get_json(path, **params))
        self._cache[cache_key] = await self._pending[cache_key]
        return self._cache[cache_key]

    async def seasons(self, show: ShowRef) -> dict[int, list[TraktEpisode]]:
        """Season number -> episodes. Empty when Trakt does not know the show:
        an unknown imdb id answers 200 with an empty list, not 404."""
        payload: list[dict[str, Any]] = await self._get(
            f"/shows/{show}/seasons", extended="episodes,full"
        ) or []
        return {
            season["number"]: [
                TraktEpisode.model_validate(episode) for episode in season.get("episodes") or []
            ]
            for season in payload
        }

    async def russian_titles(self, show: ShowRef, season: int) -> dict[int, str]:
        payload: list[dict[str, Any]] = await self._get(
            f"/shows/{show}/seasons/{season}", translations="ru"
        ) or []
        return {
            episode["number"]: translation["title"]
            for episode in payload
            for translation in episode.get("translations") or []
            if translation.get("title")
        }

    async def search_shows(self, title: str) -> list[ShowCandidate]:
        """Candidate shows for a kino.pub title, which is often "Русское / Original"."""
        parts = [part.strip() for part in (title or "").split(" / ") if part.strip()]
        results: list[list[dict[str, Any]] | None] = await asyncio.gather(
            *[self._get("/search/show", query=part) for part in parts]
        )
        candidates: dict[int, ShowCandidate] = {}
        for rows in results:
            for row in rows or []:
                show: dict[str, Any] = row.get("show") or {}
                ids: dict[str, Any] = show.get("ids") or {}
                if ids.get("trakt"):
                    candidates[ids["trakt"]] = ShowCandidate(
                        trakt=ids["trakt"],
                        imdb=ids.get("imdb"),
                        title=show.get("title"),
                        year=show.get("year"),
                    )
        return list(candidates.values())


class DecisionCache:
    """Model decisions keyed by what they depend on.

    The fingerprint covers the resolved show plus the exact kino.pub season
    composition, so a cached mapping is reused only while the inputs are
    identical — a re-plan after new episodes appear re-asks the model.
    """

    def __init__(self, cache: dict[str, Any]) -> None:
        self._cache = cache

    @staticmethod
    def fingerprint(show: ShowRef, season: int, episodes: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            [show, season, [(e["number"], e.get("title")) for e in episodes]], ensure_ascii=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def get(self, key: str) -> SeasonMatch | None:
        raw = self._cache.get(key)
        return SeasonMatch.model_validate(raw) if raw is not None else None

    def put(self, key: str, match: SeasonMatch) -> None:
        self._cache[key] = match.model_dump(mode="json")


async def resolve_show(catalog: Catalog, llm: Gemini, imdb: str, item: KinopubItem) -> ShowRef | None:
    """A usable Trakt reference: the imdb id itself, or the candidate the model
    identifies from full metadata. None when unresolvable."""
    if await catalog.seasons(imdb):
        return imdb

    candidates = await catalog.search_shows(item.title)
    if not candidates:
        return None

    answer = await llm.structured(
        SHOW_MATCH.format(
            show=json.dumps(
                {
                    "title": item.title,
                    "year": item.year,
                    "genres": [genre.title for genre in item.genres],
                    "director": item.director,
                    "cast": (item.cast or "")[:CAST_PREVIEW],
                    "plot": (item.plot or "")[:PLOT_PREVIEW],
                },
                ensure_ascii=False,
            ),
            candidates=json.dumps([c.model_dump() for c in candidates], ensure_ascii=False),
        ),
        ShowAnswer,
    )
    known = {candidate.trakt for candidate in candidates}
    return answer.trakt if answer.trakt in known else None


def _candidate_payload(
    show: ShowRef, season: int, episodes: list[TraktEpisode], russian: dict[int, str]
) -> list[dict[str, Any]]:
    return [
        {
            "show": str(show),
            "season": season,
            "episode": episode.number,
            "title": episode.title,
            "title_ru": russian.get(episode.number),
            "aired": (episode.first_aired or "")[:10],
            "runtime_min": episode.runtime,
        }
        for episode in episodes
    ]


def validate_answer(
    answer: SeasonAnswer, kp_numbers: list[int], candidates: list[dict[str, Any]]
) -> SeasonMatch:
    """Accept only rows naming a real candidate, each candidate at most once."""
    valid = {(c["show"], c["season"], c["episode"]) for c in candidates}
    match = SeasonMatch()
    taken: set[tuple[str, int, int]] = set()
    seen: set[int] = set()

    for row in answer.mapping:
        if row.kp not in kp_numbers or row.kp in seen:
            continue
        seen.add(row.kp)
        if row.unresolved or row.show is None or row.season is None or row.episode is None:
            match.unresolved[row.kp] = row.unresolved or "model gave no target"
            continue
        target = (row.show, row.season, row.episode)
        if target not in valid or target in taken:
            match.unresolved[row.kp] = "model answer failed validation"
            continue
        taken.add(target)
        match.mapping[row.kp] = Target(
            show=int(row.show) if row.show.isdigit() else row.show,
            season=row.season,
            episode=row.episode,
        )

    for number in kp_numbers:
        if number not in seen:
            match.unresolved[number] = "not covered by model answer"
    return match


async def reconcile_season(
    catalog: Catalog,
    llm: Gemini,
    cache: DecisionCache,
    *,
    show: ShowRef,
    show_title: str,
    year: int | None,
    season: int,
    kp_episodes: list[dict[str, Any]],
    trakt_seasons: dict[int, list[TraktEpisode]],
) -> SeasonMatch:
    kp_numbers = [episode["number"] for episode in kp_episodes]
    trakt_episodes = trakt_seasons.get(season) or []

    if len(kp_episodes) <= len(trakt_episodes):
        # Counts agree, or the season is still airing: numbering is trustworthy.
        return SeasonMatch(
            mapping={n: Target(show=show, season=season, episode=n) for n in kp_numbers}
        )

    fingerprint = cache.fingerprint(show, season, kp_episodes)
    if cached := cache.get(fingerprint):
        log.debug("reconcile: cache hit for %s season %s", show_title, season)
        return cached

    candidates = _candidate_payload(
        show, season, trakt_episodes, await catalog.russian_titles(show, season)
    )
    # Specials live in season 0 — of this show, and of same-titled shows, since
    # era reboots host cross-over specials.
    specials_sources: list[tuple[ShowRef, dict[int, list[TraktEpisode]]]] = [(show, trakt_seasons)]
    for candidate in await catalog.search_shows(show_title):
        if str(candidate.trakt) == str(show) or candidate.imdb == show:
            continue
        if seasons := await catalog.seasons(candidate.trakt):
            specials_sources.append((candidate.trakt, seasons))
    for source, seasons in specials_sources:
        if specials := seasons.get(0):
            candidates += _candidate_payload(
                source, 0, specials, await catalog.russian_titles(source, 0)
            )

    answer = await llm.structured(
        SEASON_MATCH.format(
            show_title=show_title,
            year=year,
            season=season,
            episodes=json.dumps(kp_episodes, ensure_ascii=False),
            candidates=json.dumps(candidates, ensure_ascii=False),
        ),
        SeasonAnswer,
    )
    match = validate_answer(answer, kp_numbers, candidates)
    cache.put(fingerprint, match)
    return match


def _kinopub_seasons(watching: KinopubWatching | None) -> dict[int, list[dict[str, Any]]]:
    """Season number -> the episode facts the model reasons over."""
    return {
        season.number: [
            {"number": episode.number, "title": episode.title, "duration": episode.duration}
            for episode in season.episodes
        ]
        for season in (watching.seasons if watching else [])
    }


async def reconcile_plan(
    plan: Plan,
    dump: Dump,
    catalog: Catalog,
    llm: Gemini,
    cache: DecisionCache,
) -> Plan:
    """Attach the true Trakt target to every episode entry. Entries whose
    identity cannot be established move to `plan.unmatched`. Shows and their
    mismatched seasons are reconciled concurrently."""
    episodic: list[EpisodeWatch | Progress] = [*plan.episodes, *plan.episode_progress]
    shows = {entry.imdb: (entry.kinopub_id, entry.title) for entry in episodic}

    targets: dict[tuple[str, int, int], Target] = {}
    reasons: dict[tuple[str, int, int], str] = {}
    notes: list[str] = []

    async def reconcile_show(imdb: str, kinopub_id: int, title: str) -> None:
        item = dump.items.get(str(kinopub_id)) or KinopubItem(id=kinopub_id, title=title)
        show = await resolve_show(catalog, llm, imdb, item)
        if show is None:
            notes.append(f"{title}: show not found on Trakt")
            return
        if show != imdb:
            notes.append(f"{title}: resolved via search -> trakt:{show}")

        kp_seasons = _kinopub_seasons(dump.watching.get(str(kinopub_id)))
        trakt_seasons = await catalog.seasons(show)
        planned = sorted({e.season for e in episodic if e.imdb == imdb and e.season is not None})

        matches = await asyncio.gather(
            *[
                reconcile_season(
                    catalog,
                    llm,
                    cache,
                    show=show,
                    show_title=title,
                    year=item.year,
                    season=season,
                    kp_episodes=kp_seasons.get(season) or [],
                    trakt_seasons=trakt_seasons,
                )
                for season in planned
            ]
        )
        for season, match in zip(planned, matches, strict=True):
            for number, target in match.mapping.items():
                targets[imdb, season, number] = target
            for number, reason in match.unresolved.items():
                reasons[imdb, season, number] = reason

    await asyncio.gather(
        *[reconcile_show(imdb, kinopub_id, title) for imdb, (kinopub_id, title) in shows.items()]
    )

    remapped = 0

    def attach(entry: EpisodeWatch | Progress) -> bool:
        nonlocal remapped
        key = (entry.imdb, entry.season or 0, entry.episode or 0)
        target = targets.get(key)
        if target is None:
            plan.unmatched.append(
                Unmatched(
                    title=entry.title,
                    reason=reasons.get(key, "show not found on Trakt"),
                    kinopub_id=entry.kinopub_id,
                    year=entry.year,
                    imdb=entry.imdb,
                    season=entry.season,
                    episode=entry.episode,
                )
            )
            return False
        entry.target = target
        if target.key != key:
            remapped += 1
        return True

    plan.episodes = [entry for entry in plan.episodes if attach(entry)]
    plan.progress = [entry for entry in plan.progress if not entry.is_episode or attach(entry)]
    plan.notes = [*notes, f"episodes remapped: {remapped}"]
    return plan
