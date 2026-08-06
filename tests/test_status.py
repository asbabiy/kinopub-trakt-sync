import time

from kinopub_trakt_sync import status as status_module
from kinopub_trakt_sync.models import EpisodeWatch, MovieWatch, Plan
from kinopub_trakt_sync.push import PushState
from kinopub_trakt_sync.settings import Paths, Settings
from kinopub_trakt_sync.storage import write_json, write_model
from kinopub_trakt_sync.verify import adopt_plan_as_state

DAY = 86400


def _settings(tmp_path):
    return Settings(paths=Paths(data_dir=tmp_path))


def _plan():
    return Plan(
        movies=[MovieWatch(kinopub_id=1, title="m", imdb="tt1", watched_at="unknown")],
        episodes=[
            EpisodeWatch(kinopub_id=2, title="s", imdb="tt2", season=1, episode=1, watched_at="unknown"),
            EpisodeWatch(kinopub_id=2, title="s", imdb="tt2", season=1, episode=2, watched_at="unknown"),
        ],
    )


def test_status_on_an_empty_directory(tmp_path):
    report = status_module.collect(_settings(tmp_path))
    assert report.pulled_at is None and report.plan is None and report.tokens == []

    text = status_module.format_status(report)
    assert "run: kts pull" in text
    assert "run: kts auth kinopub" in text


def test_pending_counts_only_unpushed_entries(tmp_path):
    settings = _settings(tmp_path)
    plan = _plan()
    write_model(settings.paths.plan, plan)

    state = PushState()
    state.record([plan.movies[0], plan.episodes[0]])
    state.save(settings.paths.push_state)

    report = status_module.collect(settings)
    assert report.pending == {"movies": 0, "episodes": 1, "progress": 0, "watchlist": 0}
    assert report.pending_total == 1
    assert "1 episodes" in status_module.format_status(report)


def test_everything_pushed_reads_as_clean(tmp_path):
    settings = _settings(tmp_path)
    plan = _plan()
    write_model(settings.paths.plan, plan)
    state = PushState()
    state.record([*plan.movies, *plan.episodes])
    state.save(settings.paths.push_state)

    text = status_module.format_status(status_module.collect(settings))
    assert "pending: nothing" in text


def test_kinopub_refresh_deadline_is_reported(tmp_path):
    settings = _settings(tmp_path)
    now = time.time()
    write_json(
        settings.paths.tokens,
        {
            "kinopub": {"obtained_at": now - 25 * DAY, "expires_in": 3600},
            "trakt": {"obtained_at": now, "expires_in": 86400},
        },
    )

    report = status_module.collect(settings)
    kinopub = next(token for token in report.tokens if token.service == "kinopub")
    trakt = next(token for token in report.tokens if token.service == "trakt")

    assert kinopub.refresh_days_left == 4  # 30-day window, 25 days used
    assert kinopub.access_valid is False  # one-hour access token, long expired
    assert trakt.access_valid is True
    assert trakt.refresh_deadline is None  # Trakt states no refresh deadline


def test_expired_kinopub_refresh_asks_for_reauthorization(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {"kinopub": {"obtained_at": time.time() - 40 * DAY, "expires_in": 3600}},
    )

    text = status_module.format_status(status_module.collect(settings))
    assert "run: kts auth kinopub" in text


def test_verified_account_repairs_stale_state_keys(tmp_path):
    """A state file written with older key formats must not leave entries
    looking unpushed forever."""
    settings = _settings(tmp_path)
    plan = _plan()
    write_model(settings.paths.plan, plan)
    stale = PushState(pushed=["progress:tt1", "some:legacy:key"])
    stale.save(settings.paths.push_state)

    assert status_module.collect(settings).pending_total == 3

    adopt_plan_as_state(plan, settings.paths.push_state)
    assert status_module.collect(settings).pending_total == 0


def test_dump_age(tmp_path):
    settings = _settings(tmp_path)
    write_json(settings.paths.dump, {"pulled_at": int(time.time() - 3 * DAY)})

    report = status_module.collect(settings)
    assert report.dump_age_days == 3
    assert "3d ago" in status_module.format_status(report)
