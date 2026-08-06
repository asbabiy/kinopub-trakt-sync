from kinopub_trakt_sync.models import KinopubItem, Target
from kinopub_trakt_sync.reconcile import (
    EpisodeAnswerRow,
    SeasonAnswer,
    ShowAnswer,
    ShowCandidate,
    TraktEpisode,
    reconcile_season,
    resolve_show,
    validate_answer,
)


class FakeCatalog:
    def __init__(self, seasons=None, russian=None, candidates=None):
        self._seasons = seasons or {}
        self._russian = russian or {}
        self._candidates = candidates or []

    async def seasons(self, show):
        return self._seasons.get(str(show), {})

    async def russian_titles(self, show, season):
        return self._russian.get((str(show), season), {})

    async def search_shows(self, title):
        return self._candidates


class FakeLLM:
    """Answers with a canned reply; asserts it is never asked when not needed."""

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = 0

    async def structured(self, prompt, schema):
        self.calls += 1
        if self.answer is None:
            raise AssertionError("model must not be consulted for a count-consistent season")
        return self.answer


class FakeCache:
    def __init__(self):
        self.stored = {}

    @staticmethod
    def fingerprint(show, season, episodes):
        return f"{show}:{season}:{len(episodes)}"

    def get(self, key):
        return self.stored.get(key)

    def put(self, key, match):
        self.stored[key] = match


SEASON_2 = [
    TraktEpisode(number=1, title="The Robot Revolution", first_aired="2025-04-12", runtime=44),
    TraktEpisode(number=2, title="Lux", first_aired="2025-04-19", runtime=44),
]
SPECIALS = [TraktEpisode(number=5, title="Joy to the World", first_aired="2024-12-25", runtime=60)]

CANDIDATES = [
    {"show": "tt1", "season": 2, "episode": 1},
    {"show": "tt1", "season": 2, "episode": 2},
    {"show": "tt1", "season": 0, "episode": 5},
]


async def test_count_consistent_season_never_calls_the_model():
    llm = FakeLLM(answer=None)
    match = await reconcile_season(
        FakeCatalog(),
        llm,
        FakeCache(),
        show="tt1",
        show_title="Шоу / Show",
        year=2024,
        season=1,
        kp_episodes=[{"number": 1, "title": "a", "duration": 100}],
        trakt_seasons={1: [TraktEpisode(number=1, title="A")]},
    )
    assert match.mapping == {1: Target(show="tt1", season=1, episode=1)}
    assert not match.unresolved
    assert llm.calls == 0


async def test_inline_special_is_remapped_to_season_zero():
    answer = SeasonAnswer(
        mapping=[
            EpisodeAnswerRow(kp=1, show="tt1", season=0, episode=5),
            EpisodeAnswerRow(kp=2, show="tt1", season=2, episode=1),
            EpisodeAnswerRow(kp=3, show="tt1", season=2, episode=2),
        ]
    )
    cache = FakeCache()
    llm = FakeLLM(answer)
    kp_episodes = [
        {"number": 1, "title": "Радуйся, мир", "duration": 3600},
        {"number": 2, "title": "Революция роботов", "duration": 2640},
        {"number": 3, "title": "Люкс", "duration": 2640},
    ]
    kwargs = {
        "show": "tt1",
        "show_title": "Доктор Кто / Doctor Who",
        "year": 2024,
        "season": 2,
        "kp_episodes": kp_episodes,
        "trakt_seasons": {2: SEASON_2, 0: SPECIALS},
    }

    match = await reconcile_season(FakeCatalog(), llm, cache, **kwargs)
    assert match.mapping == {
        1: Target(show="tt1", season=0, episode=5),
        2: Target(show="tt1", season=2, episode=1),
        3: Target(show="tt1", season=2, episode=2),
    }
    assert not match.unresolved

    # Identical inputs must reuse the cached decision instead of re-asking.
    again = await reconcile_season(FakeCatalog(), llm, cache, **kwargs)
    assert again.mapping == match.mapping
    assert llm.calls == 1


def test_targets_outside_the_candidate_set_are_rejected():
    answer = SeasonAnswer(
        mapping=[
            EpisodeAnswerRow(kp=1, show="tt1", season=2, episode=99),  # not a candidate
            EpisodeAnswerRow(kp=2, show="tt1", season=2, episode=1),
            # kp 3 is missing from the answer entirely
        ]
    )
    match = validate_answer(answer, [1, 2, 3], CANDIDATES)
    assert match.mapping == {2: Target(show="tt1", season=2, episode=1)}
    assert match.unresolved == {
        1: "model answer failed validation",
        3: "not covered by model answer",
    }


def test_two_episodes_cannot_claim_one_target():
    answer = SeasonAnswer(
        mapping=[
            EpisodeAnswerRow(kp=1, show="tt1", season=2, episode=1),
            EpisodeAnswerRow(kp=2, show="tt1", season=2, episode=1),
            EpisodeAnswerRow(kp=3, unresolved="ambiguous"),
        ]
    )
    match = validate_answer(answer, [1, 2, 3], CANDIDATES)
    assert match.mapping == {1: Target(show="tt1", season=2, episode=1)}
    assert match.unresolved == {2: "model answer failed validation", 3: "ambiguous"}


async def test_show_missing_by_imdb_is_resolved_through_search():
    catalog = FakeCatalog(
        seasons={"294690": {1: []}},
        candidates=[ShowCandidate(trakt=294690, imdb=None, title="Breathe", year=2025)],
    )
    item = KinopubItem(id=1, title="Дыши", year=2025)

    assert await resolve_show(catalog, FakeLLM(ShowAnswer(trakt=294690)), "tt30421377", item) == 294690
    assert await resolve_show(catalog, FakeLLM(ShowAnswer(trakt=None)), "tt30421377", item) is None
    # A hallucinated id that is not among the candidates is refused.
    assert await resolve_show(catalog, FakeLLM(ShowAnswer(trakt=777)), "tt30421377", item) is None
