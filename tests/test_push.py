import pytest

from kinopub_trakt_sync.push import watched_on_trakt

# Trakt nests an object under `ids.plex`; anything that treats ids as a flat
# bag of references breaks on it.
WATCHED_MOVIES = [
    {
        "movie": {
            "title": "Life",
            "ids": {
                "trakt": 1,
                "slug": "life-2017",
                "imdb": "tt5442430",
                "tmdb": 395992,
                "plex": {"guid": "5d77683f", "slug": "life-2017"},
            },
        }
    }
]

WATCHED_SHOWS = [
    {
        "show": {
            "title": "Silo",
            "ids": {"trakt": 180770, "imdb": "tt14688458", "plex": {"guid": "61b18068"}},
        },
        "seasons": [{"number": 1, "episodes": [{"number": 1}, {"number": 2}]}],
    }
]


class FakeTrakt:
    async def watched(self, media):
        return WATCHED_MOVIES if media == "movies" else WATCHED_SHOWS


@pytest.mark.asyncio
async def test_watched_index_uses_addressable_ids_only():
    movies, episodes = await watched_on_trakt(FakeTrakt())

    assert movies == {"tt5442430", 1}
    assert episodes == {
        ("tt14688458", 1, 1),
        ("tt14688458", 1, 2),
        (180770, 1, 1),
        (180770, 1, 2),
    }
