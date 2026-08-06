import asyncio

from kinopub_trakt_sync.reconcile import reconcile_season, resolve_show


class FakeCatalog:
    def __init__(self, seasons_by_ref=None, ru=None, search=None):
        self.seasons_by_ref = seasons_by_ref or {}
        self.ru = ru or {}
        self.search = search or {}

    async def seasons(self, ref):
        return self.seasons_by_ref.get(str(ref))

    async def season_ru(self, ref, season):
        return self.ru.get((str(ref), season), {})

    async def search_shows(self, query):
        return self.search.get(query, [])


class FakeLLM:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = 0

    async def generate_json(self, prompt):
        self.calls += 1
        if self.answer is None:
            raise AssertionError("model must not be called for count-consistent seasons")
        return self.answer


TRAKT_S2 = [
    {"number": 1, "title": "The Robot Revolution", "first_aired": "2025-04-12", "runtime": 44},
    {"number": 2, "title": "Lux", "first_aired": "2025-04-19", "runtime": 44},
]
TRAKT_S0 = [{"number": 5, "title": "Joy to the World", "first_aired": "2024-12-25", "runtime": 60}]


def test_count_consistent_season_skips_model():
    llm = FakeLLM(answer=None)
    mapping, unresolved = asyncio.run(reconcile_season(
        FakeCatalog(), llm, "tt1", "Шоу / Show", 2024, 1,
        [{"number": 1, "title": "a", "duration": 100}],
        {1: [{"number": 1, "title": "A", "first_aired": "", "runtime": 2}]},
    ))
    assert mapping == {1: ("tt1", 1, 1)} and unresolved == [] and llm.calls == 0


def test_shifted_season_with_inserted_special():
    kp = [
        {"number": 1, "title": "Радуйся, мир", "duration": 3600},
        {"number": 2, "title": "Революция роботов", "duration": 2640},
        {"number": 3, "title": "Люкс", "duration": 2640},
    ]
    llm = FakeLLM(
        {
            "mapping": [
                {"kp": 1, "show": "tt1", "season": 0, "episode": 5},
                {"kp": 2, "show": "tt1", "season": 2, "episode": 1},
                {"kp": 3, "show": "tt1", "season": 2, "episode": 2},
            ]
        }
    )
    mapping, unresolved = asyncio.run(reconcile_season(
        FakeCatalog(), llm, "tt1", "Доктор Кто / Doctor Who", 2024, 2,
        kp, {2: TRAKT_S2, 0: TRAKT_S0},
    ))
    assert mapping == {1: ("tt1", 0, 5), 2: ("tt1", 2, 1), 3: ("tt1", 2, 2)}
    assert unresolved == []


def test_invalid_model_targets_are_rejected_not_guessed():
    kp = [
        {"number": 1, "title": "x", "duration": 1},
        {"number": 2, "title": "y", "duration": 1},
        {"number": 3, "title": "z", "duration": 1},
    ]
    llm = FakeLLM(
        {
            "mapping": [
                {"kp": 1, "show": "tt1", "season": 2, "episode": 99},  # not a candidate
                {"kp": 2, "show": "tt1", "season": 2, "episode": 1},
                # kp 3 not covered at all
            ]
        }
    )
    mapping, unresolved = asyncio.run(reconcile_season(
        FakeCatalog(), llm, "tt1", "Шоу", 2024, 2, kp, {2: TRAKT_S2},
    ))
    assert mapping == {2: ("tt1", 2, 1)}
    assert dict(unresolved) == {
        1: "model answer failed validation",
        3: "not covered by model answer",
    }


def test_duplicate_targets_rejected():
    kp = [{"number": 1, "title": "x", "duration": 1}, {"number": 2, "title": "y", "duration": 1}, {"number": 3, "title": "z", "duration": 1}]
    llm = FakeLLM(
        {
            "mapping": [
                {"kp": 1, "show": "tt1", "season": 2, "episode": 1},
                {"kp": 2, "show": "tt1", "season": 2, "episode": 1},
                {"kp": 3, "unresolved": "ambiguous"},
            ]
        }
    )
    mapping, unresolved = asyncio.run(reconcile_season(
        FakeCatalog(), llm, "tt1", "Шоу", 2024, 2, kp, {2: TRAKT_S2},
    ))
    assert mapping == {1: ("tt1", 2, 1)}
    assert dict(unresolved) == {2: "model answer failed validation", 3: "ambiguous"}


def test_resolve_show_by_search():
    catalog = FakeCatalog(
        seasons_by_ref={"294690": [{"number": 1, "episodes": []}]},
        search={"Дыши": [{"ids": {"trakt": 294690, "imdb": None}, "title": "Breathe 2025", "year": 2025}]},
    )
    llm = FakeLLM({"trakt": 294690, "reason": "same show"})
    assert asyncio.run(resolve_show(catalog, llm, "tt30421377", {"title": "Дыши", "year": 2025})) == 294690

    llm_none = FakeLLM({"trakt": None, "reason": "no match"})
    assert asyncio.run(resolve_show(catalog, llm_none, "tt30421377", {"title": "Дыши", "year": 2025})) is None
