from kinopub_trakt_sync import transform
from kinopub_trakt_sync.models import Dump, EpisodeWatch, MovieWatch, Progress, Target, WatchlistShow
from kinopub_trakt_sync.push import history_payload, progress_payload

DUMP = Dump.model_validate(
    {
        "pulled_at": 0,
        "items": {
            "1": {"id": 1, "type": "movie", "title": "Watched Movie", "year": 2020, "imdb": 372784},
            "2": {"id": 2, "type": "movie", "title": "Half Movie", "year": 2021, "imdb": 2015381},
            "3": {"id": 3, "type": "serial", "title": "Show", "year": 2008, "imdb": 903747},
            "4": {"id": 4, "type": "tvshow", "title": "No Imdb Show", "year": 1999, "imdb": 0},
        },
        "watching": {
            "1": {
                "id": 1,
                "videos": [
                    {"number": 1, "duration": 6000, "time": 6000, "status": 1, "updated": 1600000000}
                ],
            },
            "2": {
                "id": 2,
                "videos": [
                    {"number": 1, "duration": 6000, "time": 3000, "status": 0, "updated": 1600000001}
                ],
            },
            "3": {
                "id": 3,
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
                ],
            },
            "4": {"id": 4, "seasons": []},
        },
        "watchlist": [{"id": 3, "title": "Show", "year": 2008}],
    }
)


def test_build_plan_splits_watched_progress_and_unmatched():
    plan = transform.build_plan(DUMP)
    stats = plan.stats

    assert (stats.movies, stats.episodes, stats.progress, stats.watchlist, stats.unmatched) == (
        1,
        2,
        2,
        1,
        1,
    )

    movie = plan.movies[0]
    assert movie.imdb == "tt0372784"
    assert movie.watched_at == "2020-09-13T12:26:40.000Z"

    assert [(e.season, e.episode) for e in plan.episodes] == [(1, 1), (1, 2)]
    assert plan.episodes[0].imdb == "tt0903747"
    # kino.pub has no timestamp for this one: watched, but without a date.
    assert plan.episodes[1].watched_at == "unknown"

    assert {(p.is_episode, p.percent) for p in plan.progress} == {(False, 50.0), (True, 20.0)}
    assert plan.watchlist[0].imdb == "tt0903747"
    assert plan.unmatched[0].title == "No Imdb Show"


def test_imdb_id_normalization():
    def item(value):
        return transform.KinopubItem(id=1, imdb=value)

    assert transform.imdb_id(item(903747)) == "tt0903747"
    assert transform.imdb_id(item("tt0903747")) == "tt0903747"
    assert transform.imdb_id(item(12345678)) == "tt12345678"
    assert transform.imdb_id(item(0)) is None
    assert transform.imdb_id(item(None)) is None


def test_playback_percent_stays_inside_trakt_band():
    assert transform.playback_percent(0, 6000) is None
    assert transform.playback_percent(10, 6000) is None  # below Trakt's 1% floor
    assert transform.playback_percent(6000, 6000) == 79.9  # capped below the 80% finished mark
    assert transform.playback_percent(4830, 6000) == 79.9  # 80.5% raw
    assert transform.playback_percent(3000, 0) is None


def test_history_payload_groups_by_show_and_respects_targets():
    def episode(imdb, season, number, target=None):
        return EpisodeWatch(
            kinopub_id=1,
            title="t",
            imdb=imdb,
            season=season,
            episode=number,
            watched_at="unknown",
            target=target,
        )

    payload = history_payload(
        [],
        [
            episode("tt1", 1, 1),
            episode("tt1", 1, 2),
            episode("tt1", 2, 1),
            # remapped: pushed into another show's season 0, addressed by trakt id
            episode("tt1", 2, 9, Target(show=213360, season=0, episode=5)),
        ],
    )["shows"]

    assert len(payload) == 2
    own = next(show for show in payload if show["ids"] == {"imdb": "tt1"})
    assert [season["number"] for season in own["seasons"]] == [1, 2]
    assert [ep["number"] for ep in own["seasons"][0]["episodes"]] == [1, 2]
    other = next(show for show in payload if show["ids"] == {"trakt": 213360})
    assert other["seasons"] == [{"number": 0, "episodes": [{"number": 5, "watched_at": "unknown"}]}]


def test_movie_history_payload():
    movie = MovieWatch(kinopub_id=1, title="t", imdb="tt42", watched_at="2020-01-01T00:00:00.000Z")
    assert history_payload([movie], []) == {
        "movies": [{"watched_at": "2020-01-01T00:00:00.000Z", "ids": {"imdb": "tt42"}}]
    }


def test_progress_payload_shapes():
    movie = Progress(kinopub_id=1, title="t", imdb="tt42", percent=50.0)
    assert progress_payload(movie) == {"movie": {"ids": {"imdb": "tt42"}}, "progress": 50.0}

    episode = Progress(
        kinopub_id=1,
        title="t",
        imdb="tt42",
        percent=20.0,
        season=2,
        episode=3,
        target=Target(show=99, season=0, episode=7),
    )
    assert progress_payload(episode) == {
        "show": {"ids": {"trakt": 99}},
        "episode": {"season": 0, "number": 7},
        "progress": 20.0,
    }


def test_state_keys_are_stable():
    """The push state file is the idempotency guarantee on an already synced
    account: these key formats must not drift."""
    assert MovieWatch(kinopub_id=1, title="t", imdb="tt1", watched_at="unknown").state_key == "movie:tt1"
    assert (
        EpisodeWatch(
            kinopub_id=1, title="t", imdb="tt1", season=2, episode=3, watched_at="unknown"
        ).state_key
        == "episode:tt1:2:3"
    )
    assert Progress(kinopub_id=1, title="t", imdb="tt1", percent=5).state_key == "progress:movie:tt1"
    assert (
        Progress(kinopub_id=1, title="t", imdb="tt1", percent=5, season=2, episode=3).state_key
        == "progress:tt1:2:3"
    )
    assert WatchlistShow(kinopub_id=1, title="t", imdb="tt1").state_key == "watchlist:tt1"
