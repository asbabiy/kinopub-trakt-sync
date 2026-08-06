from kinopub_trakt_sync import transform
from kinopub_trakt_sync.push import _shows_payload

DUMP = {
    "items": {
        "1": {"id": 1, "type": "movie", "title": "Watched Movie", "year": 2020, "imdb": 372784},
        "2": {"id": 2, "type": "movie", "title": "Half Movie", "year": 2021, "imdb": 2015381},
        "3": {"id": 3, "type": "serial", "title": "Show", "year": 2008, "imdb": 903747},
        "4": {"id": 4, "type": "tvshow", "title": "No Imdb Show", "year": 1999, "imdb": 0},
    },
    "watching": {
        "1": {"videos": [{"number": 1, "duration": 6000, "time": 6000, "status": 1, "updated": 1600000000}]},
        "2": {"videos": [{"number": 1, "duration": 6000, "time": 3000, "status": 0, "updated": 1600000001}]},
        "3": {
            "seasons": [
                {
                    "number": 1,
                    "episodes": [
                        {"number": 1, "duration": 3000, "time": 3000, "status": 1, "updated": 1600000100},
                        {"number": 2, "duration": 3000, "time": 3000, "status": 1, "updated": 0},
                        {"number": 3, "duration": 3000, "time": 600, "status": 0, "updated": 1600000200},
                        {"number": 4, "duration": 3000, "time": 0, "status": -1, "updated": 0},
                    ],
                }
            ]
        },
        "4": {"seasons": []},
    },
    "watchlist": [{"id": 3, "title": "Show", "year": 2008}],
}


def test_build_plan():
    plan = transform.build_plan(DUMP)

    assert plan["stats"] == {
        "movies_watched": 1,
        "episodes_watched": 2,
        "progress": 2,
        "watchlist": 1,
        "unmatched": 1,
    }

    movie = plan["movies_watched"][0]
    assert movie["imdb"] == "tt0372784"
    assert movie["watched_at"] == "2020-09-13T12:26:40.000Z"

    episodes = plan["episodes_watched"]
    assert [(e["season"], e["episode"]) for e in episodes] == [(1, 1), (1, 2)]
    assert episodes[0]["imdb"] == "tt0903747"
    assert episodes[1]["watched_at"] == "unknown"

    kinds = {(p["media"], p["percent"]) for p in plan["progress"]}
    assert kinds == {("movie", 50.0), ("episode", 20.0)}

    assert plan["watchlist"][0]["imdb"] == "tt0903747"
    assert plan["unmatched"][0]["title"] == "No Imdb Show"


def test_imdb_id_normalization():
    assert transform.imdb_id({"imdb": 903747}) == "tt0903747"
    assert transform.imdb_id({"imdb": "tt0903747"}) == "tt0903747"
    assert transform.imdb_id({"imdb": 12345678}) == "tt12345678"
    assert transform.imdb_id({"imdb": 0}) is None
    assert transform.imdb_id({"imdb": None}) is None
    assert transform.imdb_id({}) is None


def test_percent_bounds():
    assert transform._percent(0, 6000) is None
    assert transform._percent(10, 6000) is None  # below 1%
    assert transform._percent(6000, 6000) == 99.0  # capped, watched is handled by status
    assert transform._percent(3000, 0) is None


def test_shows_payload_groups_by_show_and_season():
    episodes = [
        {"imdb": "tt1", "season": 1, "episode": 1, "watched_at": "unknown"},
        {"imdb": "tt1", "season": 1, "episode": 2, "watched_at": "unknown"},
        {"imdb": "tt1", "season": 2, "episode": 1, "watched_at": "unknown"},
        {"imdb": "tt2", "season": 1, "episode": 5, "watched_at": "unknown"},
    ]
    payload = _shows_payload(episodes)
    assert len(payload) == 2
    tt1 = next(s for s in payload if s["ids"]["imdb"] == "tt1")
    assert [s["number"] for s in tt1["seasons"]] == [1, 2]
    assert [e["number"] for e in tt1["seasons"][0]["episodes"]] == [1, 2]
