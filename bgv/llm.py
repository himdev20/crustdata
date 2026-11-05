"""LLM access behind one function: complete_json(prompt, schema).

The rest of the code only calls complete_json, so swapping Gemini for another
provider means adding one class with a `generate_json(prompt, json_schema) -> str`
method and pointing BGV_LLM_PROVIDER at it. Nothing else changes.

    BGV_LLM_PROVIDER=gemini         (default)
    GEMINI_API_KEY=...              (from .env)
    GEMINI_MODEL=gemini-3.5-flash   (optional; GEMINI_FALLBACK_MODELS, GEMINI_TIMEOUT_S too)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Protocol, TypeVar, overload

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMProvider(Protocol):
    def generate_json(self, prompt: str, json_schema: dict[str, Any]) -> str:
        """Return the model's raw text, which should be a JSON document matching json_schema."""
        ...


class GeminiProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai  # imported here so other providers don't need the package

        api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (check your .env file).")
        from google.genai import types

        timeout_ms = int(os.getenv("GEMINI_TIMEOUT_S", "60")) * 1000
        # SDK retries are switched off: generate_json does its own retry + model fallback.
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        primary = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash,gemini-flash-latest")
        self.models = [primary] + [m.strip() for m in fallbacks.split(",") if m.strip() and m.strip() != primary]

    def generate_json(self, prompt: str, json_schema: dict[str, Any]) -> str:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=json_schema,
            temperature=0,
        )
        failures = []
        for model in self.models:
            for attempt in range(2):
                try:
                    resp = self.client.models.generate_content(model=model, contents=prompt, config=config)
                except errors.APIError as exc:
                    failures.append(f"{model}: {exc.code} {str(exc.message)[:120]}")
                    if (exc.code == 429 or exc.code >= 500) and attempt == 0:
                        time.sleep(3)
                        continue
                    if exc.code in (404, 429) or exc.code >= 500:
                        break  # try the next model
                    raise LLMError(f"Gemini error: {failures[-1]}") from exc
                except Exception as exc:  # timeout / connection error
                    failures.append(f"{model}: {type(exc).__name__} {str(exc)[:120]}")
                    break
                if not resp.text:
                    raise LLMError(f"Gemini returned no text (finish reason: {_finish_reason(resp)}).")
                return resp.text
        raise LLMError("All Gemini models failed: " + " | ".join(failures))


def _finish_reason(resp: Any) -> str:
    try:
        return str(resp.candidates[0].finish_reason)
    except Exception:
        return "unknown"


_PROVIDERS = {"gemini": GeminiProvider}
_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        name = os.getenv("BGV_LLM_PROVIDER", "gemini").lower()
        if name not in _PROVIDERS:
            raise LLMError(f"Unknown BGV_LLM_PROVIDER '{name}'. Known: {sorted(_PROVIDERS)}")
        _provider = _PROVIDERS[name]()
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """Override the provider (tests use this to inject a fake)."""
    global _provider
    _provider = provider


def _extract_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


@overload
def complete_json(prompt: str, schema: type[T], retries: int = ...) -> T: ...
@overload
def complete_json(prompt: str, schema: dict[str, Any], retries: int = ...) -> dict: ...


def complete_json(prompt, schema, retries: int = 1):
    """Ask the LLM for JSON matching `schema` (a pydantic model class or a JSON-schema dict).

    With a pydantic class the result is validated and returned as a model instance.
    On invalid output the error is fed back to the model and it retries once.
    """
    is_model = isinstance(schema, type) and issubclass(schema, BaseModel)
    json_schema = schema.model_json_schema() if is_model else schema
    provider = get_provider()

    attempt_prompt = prompt
    last_error = ""
    for _ in range(retries + 1):
        try:
            raw = provider.generate_json(attempt_prompt, json_schema)
        except LLMError:
            raise
        except Exception as exc:  # SDK/network errors
            raise LLMError(f"LLM call failed: {exc}") from exc
        try:
            data = _extract_json(raw)
            return schema.model_validate(data) if is_model else data
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:1000]
            attempt_prompt = (
                f"{prompt}\n\nYour previous answer was invalid:\n{last_error}\n"
                "Return only JSON that matches the schema."
            )
    raise LLMError(f"LLM output did not match the schema after {retries + 1} attempts: {last_error}")


if __name__ == "__main__":
    class _Demo(BaseModel):
        city: str
        country: str

    print(complete_json("Which city is the capital of India? Answer as JSON.", _Demo))
