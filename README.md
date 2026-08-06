# kinopub-trakt-sync

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

One-way migration of watch data from [kino.pub](https://kino.pub) to [Trakt](https://trakt.tv):

- **history** — every fully watched movie and episode, with original watch timestamps
- **progress** — playback position of unfinished movies/episodes (Trakt playback progress)
- **watchlist** — kino.pub "буду смотреть" shows into the Trakt watchlist

Movies match by IMDb id, which kino.pub stores for nearly every item. Episode
identities are *reconciled*, not trusted: kino.pub numbers specials inline
within seasons (a Christmas special may sit at position 1 and shift the whole
season, or belong to a different Trakt show entirely — era reboots), so for
every season whose episode count diverges from Trakt the true identities are
recovered by Gemini across all metadata at once — titles in both languages,
ru translations, durations vs runtimes, air dates, ordering — with every
proposed target validated against the Trakt catalog. Shows Trakt does not know
by imdb id are resolved the same way from search candidates. Anything
uncertain is reported as `unmatched`, never guessed.

## Setup

```bash
uv sync
```

Python 3.13+, `uv` for everything. The pipeline is async end to end; API
payloads are validated into pydantic models at the boundary, so anything
downstream of a client is typed.

```bash
uv run pytest && uv run ruff check src tests && uv run basedpyright
```

No registration or paid tier is needed on either side. Both services authorize
via OAuth device-code flow, whose security rests on your own sign-in rather than
on a secret client key, so both sides ship with the public client credentials
used by open-source clients — `xbmc` for kino.pub, iamkroot/trakt-scrobbler's for
Trakt. (Trakt put creating a *new* API app behind VIP in 2025, but the API
methods this tool uses are free; reusing an existing public device-flow client
sidesteps app creation entirely.) Override in `.env` only if you own a Trakt app.

Those Trakt credentials belong to another project, so its owner can revoke them
at any time — should `kts auth trakt` ever start failing with `invalid_client`,
put your own `TRAKT_CLIENT_ID` / `TRAKT_CLIENT_SECRET` into `.env`.

## Usage

Authorize once, then sync:

```bash
uv run kts auth kinopub   # prints a code to enter at kino.pub/device
uv run kts auth trakt     # prints a code to enter at trakt.tv/activate
uv run kts sync --dry-run # pull, plan, and report what would be written
uv run kts sync           # the whole pipeline, ending in a verification pass
uv run kts status         # local state: dump age, pending pushes, authorization
```

The individual stages exist for when one needs inspecting on its own:

```bash
uv run kts pull           # dump history + per-item progress -> data/kinopub_dump.json
uv run kts plan           # build reconciled sync plan + summary -> data/sync_plan.json
uv run kts push --all     # or --history / --progress / --watchlist
uv run kts verify         # element-wise audit of the account against the plan
uv run kts verify --fix   # remove wrong events, push missing ones
```

`verify` compares every play (identity and watched_at) and every playback
percent against the live account. `sync` runs it but never repairs
automatically, because repairing deletes history events.

`GEMINI_API_KEY` in `.env` is needed only when a plan actually has seasons to
match — count-consistent seasons and cached decisions never reach the model.

**Re-authorization deadline:** kino.pub invalidates a refresh token after 30
days of disuse, after which `kts auth kinopub` is required again. `kts status`
shows the days remaining.

## Behavior notes

- **Idempotent.** Trakt does not dedupe history plays, so every pushed entry is
  recorded in `data/push_state.json` and skipped on re-runs. Items already watched
  on the Trakt account (via `/sync/watched`) are skipped too.
- **Timestamps.** `watched_at` comes from the kino.pub per-episode `updated` field
  (unix time of the last status change). When kino.pub has no timestamp, the entry
  is sent with `watched_at: "unknown"` — Trakt marks it watched without a date.
  Trakt stores watched_at with minute precision (seconds are zeroed), so `verify`
  compares timestamps truncated to the minute.
- **Reads are concurrent, writes are not.** The kino.pub pull runs 8 requests at
  a time (it is a small service that throttles readily; 429s are retried with
  backoff), Trakt GETs 8 at a time within its 1000-per-5-minute budget. Every
  Trakt write stays serialized at one per second — a hard API limit, with
  `Retry-After` honoured. History is batched 500 entries per request, while
  progress costs one request per item because `/scrobble/pause` is the only
  Trakt API that sets a playback position.
- **Reconciliation is cached.** Model decisions are stored per season
  fingerprint (resolved show + exact kino.pub episode composition), so a repeat
  `plan` costs no tokens and returns the same mapping; new episodes change the
  fingerprint and re-ask.
- **`push_state.json` key formats are a compatibility surface** — they are what
  makes a re-run idempotent on an already synced account. `models.py` owns them
  and a test locks them. A verification pass that finds no differences rewrites
  the file from the plan, which also repairs keys written by older versions.
- Tokens live in `data/tokens.json` (chmod 600); the whole `data/` dir is gitignored.

## What is intentionally not transferred

- **Ratings/votes** — the kino.pub API does not expose the user's own votes at
  all: voting is write-only (`/v1/items/vote?id=&like=`, binary like/dislike),
  item payloads carry only community stats and no `my vote` field. There is
  nothing to read.
- **`counter` from history** — it is a technical player-access counter, not a
  view count: one 90-minute episode shows counter=339 within a single day,
  ordinary single-sitting movies show 1–2. kino.pub has no rewatch data at
  all (history keeps one record per episode/video; re-watching just bumps
  last_seen/counter), so mapping counter to Trakt plays would fabricate
  rewatch history.
- **`first_seen`** — Trakt stores one datetime per play; the completion moment
  (`updated`) is what "watched at" means.

## kino.pub API surface (probed, not just documented)

The API was probed beyond its docs (~130 candidate paths). **Mutations in this
API are performed via GET** (`/v1/items/vote`, `/v1/watching/toggle`,
`/v1/watching/marktime`, `/v1/history/clear-*`), so any probing must exclude
write-verb paths — a blind sweep corrupts the account.

Result: no undocumented personal-data endpoint exists. Everything carrying a
user signal is already consumed by `pull`:

| Endpoint | Personal signal |
|---|---|
| `/v1/history` | per item: `time`, `first_seen`, `last_seen`, `counter` |
| `/v1/watching?id=` | per episode: `status`, `time`, `updated` |
| `/v1/watching/movies`, `/v1/watching/serials` | unfinished items, watchlist |
| `/v1/bookmarks`, item field `bookmarks` | user-created folders |
| `/v1/user` | username, reg date, subscription, `show_erotic`/`show_uncertain` |
| `/v1/device` | device list with `last_seen` — session metadata, not watch data |

Absent everywhere: the user's own votes (item payloads expose only community
`rating`/`rating_percentage`/`rating_votes`; `views` is global), any per-user
play count, and any rewatch log. `/v1/history` ignores `type`/`media` filters
and caps `perpage` at 50. No `/v2`, no stats, notifications, or
recommendations endpoints. `/v1/items/{id}` additionally serves `similar`,
`comments`, and `trailer` — all catalog data.

Field semantics worth knowing when reading the code: `/v1/watching?id=` reports
per-episode `status` as `-1` unwatched, `0` in progress, `1` watched, alongside
`time` (position in seconds) and `updated` (unix time of the last status
change). Authorization is an OAuth2 device-code flow against `/oauth2/device`.
The official documentation lives at <https://kinoapi.com>.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with kino.pub or Trakt.
