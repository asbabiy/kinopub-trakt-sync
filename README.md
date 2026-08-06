# kinopub-trakt-sync

One-way migration of watch data from [kino.pub](https://kino.pub) to [Trakt](https://trakt.tv):

- **history** — every fully watched movie and episode, with original watch timestamps
- **progress** — playback position of unfinished movies/episodes (Trakt playback progress)
- **watchlist** — kino.pub "буду смотреть" shows into the Trakt watchlist

Matching is by IMDb id, which kino.pub stores for nearly every item. Items without
one are reported as `unmatched` for manual review, never guessed.

## Setup

```bash
uv sync
cp .env.example .env   # fill in Trakt app credentials
```

Trakt requires your own API app: create one at <https://trakt.tv/oauth/applications>
(redirect uri `urn:ietf:wg:oauth:2.0:oob`), put its client id/secret into `.env`.
kino.pub needs no registration — the shared public client credentials used by
open-source clients (`xbmc`) are built in.

## Usage

```bash
uv run kts auth kinopub   # prints a code to enter at kino.pub/device
uv run kts auth trakt     # prints a code to enter at trakt.tv/activate
uv run kts pull           # dump history + per-item progress -> data/kinopub_dump.json
uv run kts plan           # build sync plan + summary      -> data/sync_plan.json
uv run kts push --all --dry-run
uv run kts push --all
```

`push` accepts `--history`, `--progress`, `--watchlist` individually.

## Behavior notes

- **Idempotent.** Trakt does not dedupe history plays, so every pushed entry is
  recorded in `data/push_state.json` and skipped on re-runs. Items already watched
  on the Trakt account (via `/sync/watched`) are skipped too.
- **Timestamps.** `watched_at` comes from the kino.pub per-episode `updated` field
  (unix time of the last status change). When kino.pub has no timestamp, the entry
  is sent with `watched_at: "unknown"` — Trakt marks it watched without a date.
- **Progress** is pushed via `/scrobble/pause` (the only Trakt API that sets
  playback position), one request per item at 1 rps.
- **Rate limits.** Trakt POSTs run at 1/s with `Retry-After` handling; history is
  batched 500 entries per request. The kino.pub pull sleeps 200 ms between items.
- Tokens live in `data/tokens.json` (chmod 600); the whole `data/` dir is gitignored.

## kino.pub API notes

Documented at <https://kinoapi.com>. Endpoints used: `/v1/history` (paginated view
log), `/v1/items/{id}` (metadata incl. IMDb id), `/v1/watching?id=` (per-episode
status/position: `-1` unwatched, `0` in progress, `1` watched, plus `time` and
`updated`), `/v1/watching/movies`, `/v1/watching/serials?subscribed=1` (watchlist).
Auth is an OAuth2 device-code flow against `/oauth2/device`.
