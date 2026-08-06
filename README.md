# kinopub-trakt-sync

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

No registration or paid tier is needed on either side. Both services authorize
via OAuth device-code flow, whose security rests on your own sign-in rather than
on a secret client key, so both sides ship with the public client credentials
used by open-source clients — `xbmc` for kino.pub, iamkroot/trakt-scrobbler's for
Trakt. (Trakt put creating a *new* API app behind VIP in 2025, but the API
methods this tool uses are free; reusing an existing public device-flow client
sidesteps app creation entirely.) Override in `.env` only if you own a Trakt app.

## Usage

```bash
uv run kts auth kinopub   # prints a code to enter at kino.pub/device
uv run kts auth trakt     # prints a code to enter at trakt.tv/activate
uv run kts pull           # dump history + per-item progress -> data/kinopub_dump.json
uv run kts plan           # build reconciled sync plan + summary -> data/sync_plan.json
uv run kts push --all --dry-run
uv run kts push --all
uv run kts verify         # element-wise audit of the account against the plan
uv run kts verify --fix   # remove wrong events, push missing ones
```

`push` accepts `--history`, `--progress`, `--watchlist` individually.
`plan` needs `GEMINI_API_KEY` in `.env` for the identity reconciliation.
`verify` compares every play (identity and watched_at) and every playback
percent against the live account and reports missing / extra / mismatched.

## Behavior notes

- **Idempotent.** Trakt does not dedupe history plays, so every pushed entry is
  recorded in `data/push_state.json` and skipped on re-runs. Items already watched
  on the Trakt account (via `/sync/watched`) are skipped too.
- **Timestamps.** `watched_at` comes from the kino.pub per-episode `updated` field
  (unix time of the last status change). When kino.pub has no timestamp, the entry
  is sent with `watched_at: "unknown"` — Trakt marks it watched without a date.
  Trakt stores watched_at with minute precision (seconds are zeroed), so `verify`
  compares timestamps truncated to the minute.
- **Concurrency.** All reads are concurrent: the kino.pub pull (semaphore 16 —
  it is a small service, more invites 429s/bans), Trakt catalog/history GETs
  (semaphore 8 within Trakt's 1000-per-5-min budget), and Gemini season
  matching. Trakt POSTs stay serialized at 1 rps — a hard API limit.
- **Progress** is pushed via `/scrobble/pause` (the only Trakt API that sets
  playback position), one request per item at 1 rps.
- **Rate limits.** Trakt POSTs run at 1/s with `Retry-After` handling; history is
  batched 500 entries per request. The kino.pub pull sleeps 200 ms between items.
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

## kino.pub API notes

Documented at <https://kinoapi.com>. Endpoints used: `/v1/history` (paginated view
log), `/v1/items/{id}` (metadata incl. IMDb id), `/v1/watching?id=` (per-episode
status/position: `-1` unwatched, `0` in progress, `1` watched, plus `time` and
`updated`), `/v1/watching/movies`, `/v1/watching/serials?subscribed=1` (watchlist).
Auth is an OAuth2 device-code flow against `/oauth2/device`.
