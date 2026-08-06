"""Gemini transport: schema-constrained generation for matching decisions.

The response schema is a pydantic model, so google-genai returns a validated
instance and no shape-guessing is needed at the call site. The content still
counts as untrusted: reconcile.py checks every proposed target against the
Trakt catalog before anything is written.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .settings import Settings

log = logging.getLogger(__name__)


class MatcherError(RuntimeError):
    pass


class Gemini:
    """The client is built on first use, not at construction.

    Most runs need no matching at all — seasons whose episode counts agree, and
    cached decisions, never reach the model — so requiring an API key upfront
    would fail plans that were never going to call it.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: genai.Client | None = None

    def _connect(self) -> genai.Client:
        if self._client is None:
            if not self._settings.gemini_api_key:
                raise MatcherError(
                    "this plan needs episode matching, which requires GEMINI_API_KEY in .env"
                )
            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    @retry(
        retry=retry_if_exception_type(MatcherError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def structured[T: BaseModel](self, prompt: str, schema: type[T]) -> T:
        response = await self._connect().aio.models.generate_content(
            model=self._settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, schema):
            log.warning("gemini returned no parsable %s", schema.__name__)
            raise MatcherError(f"model returned no valid {schema.__name__}")
        return parsed
