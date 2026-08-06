"""Prompts for identity reconciliation.

Both prompts state the failure mode they exist to prevent, because that is what
the model has to reason about: kino.pub's inline numbering of specials, and
Russian titles being translations rather than transliterations. The answer
shape is enforced by a response schema, so it is not spelled out here.
"""

from __future__ import annotations

SHOW_MATCH = """Identify which Trakt show (if any) is the same show as the kino.pub one.

kino.pub show metadata:
{show}

Trakt candidates:
{candidates}

Answer with the trakt id of the matching candidate, or null when unsure. Match only if
certain it is the same show: the title must be a plausible translation or transliteration
of a candidate, and the year must agree. Do not guess."""

SEASON_MATCH = """Map each kino.pub episode of one season to its true Trakt episode identity.

Background: kino.pub numbers specials inline within a season — a special may sit at
position 1 and shift every later episode by one, or be appended at the season tail.
Trakt instead keeps specials in season 0, sometimes season 0 of a related show (an era
reboot). Russian titles are translations and may differ in wording from Trakt's own
Russian titles.

Use every signal jointly: titles in both languages, durations against runtimes, air
dates, and ordering — kino.pub preserves airing order within a season.

kino.pub show: {show_title} ({year}), season {season}. Episodes (duration in seconds):
{episodes}

Trakt candidate episodes:
{candidates}

Return one row per kino.pub episode, each appearing exactly once. Fill show/season/episode
exactly as given in the candidates for a confident match; otherwise leave them empty and
give a short reason instead. Never map two kino.pub episodes to the same Trakt episode.
Do not guess."""
