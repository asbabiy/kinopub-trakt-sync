"""Recover true Trakt episode identities behind kino.pub season numbering.

kino.pub numbers specials inline: a Christmas special can sit at position 1
(shifting the whole season) or at the season tail, and era-reboot specials may
even belong to a different Trakt show. Trakt keeps specials in season 0.
Relying on raw season/episode numbers therefore silently mislabels episodes
whenever per-season episode counts diverge.

Division of labor:
- code fetches the ground truth deterministically (Trakt season structures,
  ru translations, air dates, runtimes; candidate shows via search) and
  validates every proposed target against it;
- Gemini decides the matching itself — across ALL metadata at once (titles in
  both languages with translation variance, durations vs runtimes, air dates,
  ordering), for season mappings and for resolving shows Trakt does not know
  by imdb id.

Seasons whose episode counts agree are trusted as-is and never sent to the
model. Anything the model cannot certainly place, or that fails validation,
is reported as unresolved — never guessed.
"""

import asyncio
import json

from .gemini import Gemini


class Catalog:
    """Cached read-only view of Trakt show structures (cache survives runs).

    Concurrent lookups of the same key share one in-flight request.
    """

    def __init__(self, client, cache: dict):
        self.client = client
        self.cache = cache
        self._pending: dict = {}

    async def _get(self, path: str, **params):
        key = path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
        if key in self.cache:
            return self.cache[key]
        if key not in self._pending:
            self._pending[key] = asyncio.create_task(self.client.get_json(path, **params))
        self.cache[key] = await self._pending[key]
        return self.cache[key]

    async def seasons(self, show_ref) -> list | None:
        """[{number, episodes: [{number, title, first_aired, runtime}]}] or None."""
        data = await self._get(f"/shows/{show_ref}/seasons", extended="episodes,full")
        if data is None:
            return None
        return [
            {
                "number": s["number"],
                "episodes": [
                    {
                        "number": e["number"],
                        "title": e.get("title"),
                        "first_aired": e.get("first_aired"),
                        "runtime": e.get("runtime"),
                    }
                    for e in s.get("episodes") or []
                ],
            }
            for s in data
        ]

    async def season_ru(self, show_ref, season: int) -> dict[int, str]:
        """Episode number -> Russian title for one season ({} if none)."""
        data = await self._get(f"/shows/{show_ref}/seasons/{season}", translations="ru")
        result = {}
        for ep in data or []:
            for tr in ep.get("translations") or []:
                if tr.get("title"):
                    result[ep["number"]] = tr["title"]
        return result

    async def search_shows(self, query: str) -> list:
        data = await self._get("/search/show", query=query) or []
        return [x["show"] for x in data if "show" in x]


async def _search_candidates(catalog: Catalog, title: str) -> list[dict]:
    parts = [p.strip() for p in (title or "").split(" / ") if p.strip()]
    results = await asyncio.gather(*[catalog.search_shows(p) for p in parts])
    seen = {}
    for shows in results:
        for show in shows:
            tid = show["ids"].get("trakt")
            if tid:
                seen[tid] = {
                    "trakt": tid,
                    "imdb": show["ids"].get("imdb"),
                    "title": show.get("title"),
                    "year": show.get("year"),
                }
    return list(seen.values())


async def resolve_show(catalog: Catalog, llm: Gemini, imdb: str, item_meta: dict) -> str | int | None:
    """Usable Trakt show reference: the imdb id itself, or — when Trakt does
    not know it — the candidate the model identifies from full metadata.
    None if unresolvable."""
    # An unknown imdb id yields an empty season list (200), not a 404.
    if await catalog.seasons(imdb):
        return imdb
    candidates = await _search_candidates(catalog, item_meta.get("title") or "")
    if not candidates:
        return None
    prompt = (
        "Identify which Trakt show (if any) is the same show as the kino.pub one.\n"
        "kino.pub show metadata:\n"
        + json.dumps(
            {
                "title": item_meta.get("title"),
                "year": item_meta.get("year"),
                "genres": [g.get("title") for g in item_meta.get("genres") or []],
                "director": item_meta.get("director"),
                "cast": (item_meta.get("cast") or "")[:200],
                "plot": (item_meta.get("plot") or "")[:400],
            },
            ensure_ascii=False,
        )
        + "\nTrakt candidates:\n"
        + json.dumps(candidates, ensure_ascii=False)
        + '\nReturn JSON: {"trakt": <trakt id of the matching candidate or null>, "reason": "<short>"}\n'
        "Match only if certain it is the same show (title translation and year must agree)."
    )
    answer = await llm.generate_json(prompt)
    if isinstance(answer, list):
        answer = answer[0] if answer and isinstance(answer[0], dict) else {}
    chosen = answer.get("trakt") if isinstance(answer, dict) else None
    if chosen in {c["trakt"] for c in candidates}:
        return chosen
    return None


def _season_payload(show_ref, season_number: int, episodes: list, ru: dict) -> list[dict]:
    return [
        {
            "show": str(show_ref),
            "season": season_number,
            "episode": e["number"],
            "title": e.get("title"),
            "title_ru": ru.get(e["number"]),
            "aired": (e.get("first_aired") or "")[:10],
            "runtime_min": e.get("runtime"),
        }
        for e in episodes
    ]


async def reconcile_season(
    catalog: Catalog,
    llm: Gemini,
    show_ref,
    show_title: str,
    year,
    season_number: int,
    kp_eps: list,  # [{number, title, duration}] — the full kino.pub season
    trakt_seasons: dict,  # season number -> episodes, for the resolved show
):
    """Return (mapping, unresolved) for one season.

    mapping: kp episode number -> (show_ref, season, episode)
    unresolved: [(kp number, reason)]
    """
    kp_numbers = [e["number"] for e in kp_eps]
    trakt_eps = trakt_seasons.get(season_number) or []
    if len(kp_eps) <= len(trakt_eps):
        # Counts agree, or the season is still airing: numbering is trustworthy.
        return {n: (show_ref, season_number, n) for n in kp_numbers}, []

    # Candidates: this season, its own specials, and specials of same-titled
    # shows (era reboots often host cross-over specials).
    candidates = _season_payload(
        show_ref, season_number, trakt_eps, await catalog.season_ru(show_ref, season_number)
    )
    specials_refs = [(show_ref, trakt_seasons)]
    for cand in await _search_candidates(catalog, show_title):
        ref = cand["trakt"]
        if str(ref) != str(show_ref) and cand.get("imdb") != show_ref:
            seasons = await catalog.seasons(ref)
            if seasons:
                specials_refs.append((ref, {s["number"]: s["episodes"] for s in seasons}))
    for ref, seasons in specials_refs:
        if seasons.get(0):
            candidates += _season_payload(ref, 0, seasons[0], await catalog.season_ru(ref, 0))

    prompt = (
        "Map each kino.pub episode of one season to its true Trakt episode identity.\n"
        "Background: kino.pub numbers specials inline within a season (a special may sit at "
        "position 1 and shift the whole season, or be appended at the tail), while Trakt keeps "
        "specials in season 0 — sometimes season 0 of a related show (era reboot). Russian "
        "titles are translations and may differ in wording from Trakt's ru titles.\n"
        "Use every signal jointly: titles in both languages, durations vs runtimes, air dates, "
        "and ordering (kino.pub preserves airing order within a season).\n"
        f"kino.pub show: {show_title} ({year}), season {season_number}, episodes "
        "(duration is in seconds):\n"
        + json.dumps(kp_eps, ensure_ascii=False)
        + "\nTrakt candidate episodes:\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\nReturn JSON: {\"mapping\": [{\"kp\": <kino.pub episode number>, \"show\": <show>, "
        '"season": <season>, "episode": <episode>} for confident matches, or '
        '{"kp": <number>, "unresolved": "<short reason>"} when identity is not certain]}.\n'
        "Every kino.pub episode must appear exactly once; never map two kino.pub episodes to "
        "the same Trakt episode; use show/season/episode values exactly as given in candidates. "
        "Do not guess."
    )
    answer = await llm.generate_json(prompt)
    rows = answer.get("mapping", []) if isinstance(answer, dict) else answer
    if not isinstance(rows, list):
        rows = []

    valid_targets = {(c["show"], c["season"], c["episode"]) for c in candidates}
    mapping: dict[int, tuple] = {}
    unresolved: list[tuple] = []
    taken: set[tuple] = set()
    seen_kp: set[int] = set()
    for row in rows:
        kp = row.get("kp")
        if kp not in kp_numbers or kp in seen_kp:
            continue
        seen_kp.add(kp)
        if "unresolved" in row:
            unresolved.append((kp, row["unresolved"]))
            continue
        target = (str(row.get("show")), row.get("season"), row.get("episode"))
        if target not in valid_targets or target in taken:
            unresolved.append((kp, "model answer failed validation"))
            continue
        taken.add(target)
        show = target[0]
        mapping[kp] = (int(show) if show.isdigit() else show, target[1], target[2])
    for kp in kp_numbers:
        if kp not in seen_kp:
            unresolved.append((kp, "not covered by model answer"))
    return mapping, unresolved


async def reconcile_plan(plan: dict, dump: dict, catalog: Catalog, llm: Gemini) -> dict:
    """Attach a 'target' (true Trakt show/season/episode) to every episode
    entry of the plan. Entries whose identity cannot be established move to
    plan['unmatched']. Shows and their mismatched seasons run concurrently."""
    shows: dict[str, dict] = {}
    for entry in plan["episodes_watched"] + [p for p in plan["progress"] if p["media"] == "episode"]:
        shows.setdefault(entry["imdb"], {"kid": entry["kinopub_id"], "title": entry["title"]})

    remap: dict[tuple, tuple] = {}
    dropped: dict[tuple, str] = {}
    notes: list[str] = []

    async def reconcile_show(imdb: str, info: dict) -> None:
        item_meta = dump["items"].get(str(info["kid"])) or {}
        ref = await resolve_show(catalog, llm, imdb, item_meta)
        if ref is None:
            notes.append(f"{info['title']}: show not found on Trakt")
            return
        if ref != imdb:
            notes.append(f"{info['title']}: resolved via search -> trakt:{ref}")

        watching = dump["watching"].get(str(info["kid"])) or {}
        kp_seasons = {
            s["number"]: [
                {"number": e["number"], "title": e.get("title"), "duration": e.get("duration")}
                for e in (s.get("episodes") or [])
            ]
            for s in (watching.get("seasons") or [])
        }
        trakt_seasons = {s["number"]: s["episodes"] for s in await catalog.seasons(ref) or []}
        planned_seasons = sorted(
            {
                e["season"]
                for e in plan["episodes_watched"] + plan["progress"]
                if e.get("imdb") == imdb and "season" in e
            }
        )
        results = await asyncio.gather(
            *[
                reconcile_season(
                    catalog, llm, ref, info["title"], item_meta.get("year"), sn,
                    kp_seasons.get(sn) or [], trakt_seasons,
                )
                for sn in planned_seasons
            ]
        )
        for sn, (mapping, unresolved) in zip(planned_seasons, results):
            for n, tgt in mapping.items():
                remap[(imdb, sn, n)] = tgt
            for n, reason in unresolved:
                dropped[(imdb, sn, n)] = reason

    await asyncio.gather(*[reconcile_show(imdb, info) for imdb, info in shows.items()])

    remapped_count = 0

    def attach(entry: dict) -> dict | None:
        nonlocal remapped_count
        key = (entry["imdb"], entry["season"], entry["episode"])
        if key not in remap:
            plan["unmatched"].append({**entry, "reason": dropped.get(key, "show not found on Trakt")})
            return None
        show, s, e = remap[key]
        if (show, s, e) != (entry["imdb"], entry["season"], entry["episode"]):
            remapped_count += 1
        return {**entry, "target": {"show": show, "season": s, "episode": e}}

    plan["episodes_watched"] = [x for x in (attach(e) for e in plan["episodes_watched"]) if x]
    plan["progress"] = [
        x for x in (attach(p) if p["media"] == "episode" else p for p in plan["progress"]) if x
    ]
    plan["reconcile_notes"] = notes + [f"episodes remapped: {remapped_count}"]
    plan["stats"]["episodes_watched"] = len(plan["episodes_watched"])
    plan["stats"]["progress"] = len(plan["progress"])
    plan["stats"]["unmatched"] = len(plan["unmatched"])
    return plan
