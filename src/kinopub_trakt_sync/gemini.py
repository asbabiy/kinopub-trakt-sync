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
    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise MatcherError("GEMINI_API_KEY not set — put it in .env")
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    @retry(
        retry=retry_if_exception_type(MatcherError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def structured[T: BaseModel](self, prompt: str, schema: type[T]) -> T:
        response = await self._client.aio.models.generate_content(
            model=self._model,
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
