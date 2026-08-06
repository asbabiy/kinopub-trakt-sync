"""Gemini transport: structured-JSON generation for matching decisions."""

import json

from google import genai
from google.genai import types

from . import config


class MatcherError(RuntimeError):
    pass


class Gemini:
    def __init__(self) -> None:
        if not config.GEMINI_API_KEY:
            raise MatcherError("GEMINI_API_KEY not set in .env")
        self._client = genai.Client(api_key=config.GEMINI_API_KEY)

    async def generate_json(self, prompt: str) -> dict:
        last_error = None
        for _ in range(2):
            response = await self._client.aio.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            try:
                return json.loads(response.text)
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc
        raise MatcherError(f"unparsable model response: {last_error}")
