"""Domain model of a sync plan.

A plan is the whole contract between the read side (kino.pub dump) and the
write side (Trakt): what was watched, when, how far, and — after
reconciliation — under which Trakt identity.

`imdb`/`season`/`episode` always describe the kino.pub view of an item; the
optional `target` describes where the item actually lives on Trakt. The two
differ whenever kino.pub numbers specials inline (see reconcile.py).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

# A Trakt show is addressed either by imdb id ("tt0903747") or, when Trakt does
# not know that id, by its numeric trakt id.
type ShowRef = str | int

# kino.pub per-episode status.
UNWATCHED = -1
IN_PROGRESS = 0
WATCHED = 1


class KinopubPayload(BaseModel):
    """kino.pub sends far more fields than are used here; ignore the rest."""

    model_config = ConfigDict(extra="ignore")


class KinopubVideo(KinopubPayload):
    """One playable video of a movie, or one episode."""

    number: int
    title: str | None = None
    duration: int | None = None
    time: int | None = None
    status: int | None = None
    updated: int | None = None


class KinopubSeason(KinopubPayload):
    number: int
    episodes: list[KinopubVideo] = []


class KinopubWatching(KinopubPayload):
    """Watch state of one item: /v1/watching?id=."""

    id: int
    title: str = ""
    type: str = ""
    seasons: list[KinopubSeason] = []
    videos: list[KinopubVideo] = []


class KinopubGenre(KinopubPayload):
    title: str | None = None


class KinopubItem(KinopubPayload):
    """Catalog metadata, as embedded in history records and /v1/items/{id}."""

    id: int
    type: str = ""
    title: str = ""
    year: int | None = None
    imdb: int | str | None = None
    genres: list[KinopubGenre] = []
    director: str | None = None
    cast: str | None = None
    plot: str | None = None


class KinopubWatchlistEntry(KinopubPayload):
    id: int
    title: str = ""
    year: int | None = None


class Dump(BaseModel):
    """Everything pulled from kino.pub, as stored in data/kinopub_dump.json.

    `history` stays opaque: nothing downstream reads it — item metadata is
    lifted out of it at pull time — but it is the raw record of the source and
    is kept verbatim.
    """

    pulled_at: int
    history: list[dict[str, Any]] = []
    items: dict[str, KinopubItem] = {}
    watching: dict[str, KinopubWatching] = {}
    watchlist: list[KinopubWatchlistEntry] = []


class Target(BaseModel):
    """True Trakt identity of an episode."""

    model_config = ConfigDict(frozen=True)

    show: ShowRef
    season: int
    episode: int

    @property
    def ids(self) -> dict[str, ShowRef]:
        """Trakt `ids` object addressing this show."""
        return {"trakt": self.show} if isinstance(self.show, int) else {"imdb": self.show}

    @property
    def key(self) -> tuple[ShowRef, int, int]:
        return (self.show, self.season, self.episode)


class Watch(BaseModel):
    """Fields shared by everything a plan can carry."""

    kinopub_id: int
    title: str
    year: int | None = None
    imdb: str


class MovieWatch(Watch):
    watched_at: str

    @computed_field
    @property
    def state_key(self) -> str:
        return f"movie:{self.imdb}"


class EpisodeWatch(Watch):
    season: int
    episode: int
    watched_at: str
    target: Target | None = None

    @computed_field
    @property
    def state_key(self) -> str:
        # Keyed by kino.pub identity so the key survives a re-plan that remaps
        # the entry onto a different Trakt target.
        return f"episode:{self.imdb}:{self.season}:{self.episode}"

    @property
    def trakt(self) -> Target:
        return self.target or Target(show=self.imdb, season=self.season, episode=self.episode)


class Progress(Watch):
    """Playback position of an unfinished movie or episode."""

    percent: float
    season: int | None = None
    episode: int | None = None
    target: Target | None = None

    @property
    def is_episode(self) -> bool:
        return self.season is not None

    @computed_field
    @property
    def state_key(self) -> str:
        if not self.is_episode:
            return f"progress:movie:{self.imdb}"
        return f"progress:{self.imdb}:{self.season}:{self.episode}"

    @property
    def trakt(self) -> Target | None:
        if not self.is_episode:
            return None
        return self.target or Target(show=self.imdb, season=self.season or 0, episode=self.episode or 0)


class WatchlistShow(Watch):
    @computed_field
    @property
    def state_key(self) -> str:
        return f"watchlist:{self.imdb}"


class Unmatched(BaseModel):
    """An item deliberately left out, with the reason — never silently dropped."""

    title: str
    reason: str
    kinopub_id: int | None = None
    year: int | None = None
    imdb: str | None = None
    season: int | None = None
    episode: int | None = None


class PlanStats(BaseModel):
    movies: int
    episodes: int
    shows: int
    progress: int
    watchlist: int
    unmatched: int


class Plan(BaseModel):
    movies: list[MovieWatch] = []
    episodes: list[EpisodeWatch] = []
    progress: list[Progress] = []
    watchlist: list[WatchlistShow] = []
    unmatched: list[Unmatched] = []
    notes: list[str] = []

    @property
    def episode_progress(self) -> list[Progress]:
        return [entry for entry in self.progress if entry.is_episode]

    @computed_field
    @property
    def stats(self) -> PlanStats:
        return PlanStats(
            movies=len(self.movies),
            episodes=len(self.episodes),
            shows=len({entry.imdb for entry in self.episodes}),
            progress=len(self.progress),
            watchlist=len(self.watchlist),
            unmatched=len(self.unmatched),
        )
