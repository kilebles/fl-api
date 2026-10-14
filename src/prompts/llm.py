"""One structured-output call for both providers: Gemini for `gemini-*` models, OpenAI for `gpt-*`."""

import asyncio
from typing import Any

from google import genai
from google.genai import types as genai_types
from loguru import logger
from openai import AsyncOpenAI, omit

RETRY_DELAYS = (3.0, 10.0, 30.0)


def _retryable(e: Exception) -> bool:
    """Rate limits, timeouts, 5xx and network errors are worth retrying; other 4xx are not."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return not (isinstance(code, int) and 400 <= code < 500 and code not in (408, 429))


class LLMNotConfiguredError(RuntimeError):
    """The API key for the chosen provider is not set."""


class LLMClient:
    """
    `json(model, prompt, schema)` returns the model's JSON text for a JSON schema.

    Rate limits, 5xx and timeouts are retried with growing pauses; other client
    errors (bad key, bad request) and a missing key fail at once.
    """

    def __init__(self, google_api_key: str, openai_api_key: str) -> None:
        self._gemini = genai.Client(api_key=google_api_key) if google_api_key else None
        self._openai = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None

    async def json(
        self,
        model: str,
        prompt: str,
        schema: dict[str, Any] | type,
        temperature: float,
        system: str | None = None,
        name: str = "response",
    ) -> str:
        for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
            try:
                if model.startswith("gpt-"):
                    return await self._openai_json(model, prompt, schema, temperature, system, name)
                return await self._gemini_json(model, prompt, schema, temperature, system)
            except LLMNotConfiguredError:
                raise
            except Exception as e:
                if delay is None or not _retryable(e):
                    raise
                logger.warning(f"[llm] {model} call failed (attempt {attempt}): {type(e).__name__}: {e} — retry in {delay:.0f}s")
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _gemini_json(self, model: str, prompt: str, schema: dict[str, Any] | type, temperature: float, system: str | None) -> str:
        if self._gemini is None:
            raise LLMNotConfiguredError("GOOGLE_API_KEY is not set")
        schema_arg = {"response_json_schema": schema} if isinstance(schema, dict) else {"response_schema": schema}
        response = await self._gemini.aio.models.generate_content(
            model=model,
            contents=f"{system}\n\n---\n\n{prompt}" if system else prompt,
            config=genai_types.GenerateContentConfig(response_mime_type="application/json", temperature=temperature, **schema_arg),
        )
        return response.text or "{}"

    async def _openai_json(self, model: str, prompt: str, schema: dict[str, Any] | type, temperature: float, system: str | None, name: str) -> str:
        if self._openai is None:
            raise LLMNotConfiguredError("OPENAI_API_KEY is not set")
        if not isinstance(schema, dict):
            raise TypeError("OpenAI calls need a JSON schema dict")
        messages: list[Any] = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        response = await self._openai.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},  # type: ignore[arg-type]
            # gpt-5.x accept only the default temperature and reject any explicit value
            temperature=omit if model.startswith("gpt-5") else temperature,
        )
        return response.choices[0].message.content or "{}"
