"""The only module that talks to an LLM provider.

The rest of the app calls complete_json() and catches LLMError, so swapping Gemini
for Anthropic means changing this file alone. The LLM is used for resume parsing
and the report summary only, never for verdicts.
"""
from __future__ import annotations

import json
import os
import random
import time
from functools import lru_cache
from typing import TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

load_dotenv()

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_RATE_LIMIT_RETRIES = 4
BASE_BACKOFF_SECONDS = 2.0

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The LLM call failed, or its output never matched the schema."""


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise LLMError("GEMINI_API_KEY is not set (check your .env file).")
    return genai.Client(api_key=api_key)


def _generate(prompt: str, schema: type[BaseModel]) -> str:
    """Return the model's raw text, backing off and retrying on 429 only."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=schema.model_json_schema(),
        temperature=0,
    )
    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = _client().models.generate_content(
                model=model, contents=prompt, config=config
            )
            # .text is None when the response is blocked or empty; validation
            # then fails and takes the normal retry path.
            return response.text or ""
        except errors.APIError as exc:
            if exc.code != 429:
                raise LLMError(f"Gemini request failed ({exc.code}): {exc.message}") from exc
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise LLMError(
                    f"Gemini still rate limited after {MAX_RATE_LIMIT_RETRIES} retries."
                ) from exc
            delay = BASE_BACKOFF_SECONDS * 2**attempt + random.uniform(0, 1)
            time.sleep(delay)

    raise AssertionError("unreachable")


def complete_json(prompt: str, schema: type[T]) -> T:
    """Ask for JSON matching `schema` and return it as a validated instance.

    If the first answer fails validation, retry once with the error attached.
    Raises LLMError if the call fails or the second answer is also invalid.
    """
    text = _generate(prompt, schema)
    try:
        return schema.model_validate_json(text)
    except ValidationError as first_error:
        retry_prompt = (
            f"{prompt}\n\n"
            "Your previous answer did not match the required JSON schema.\n"
            f"Previous answer:\n{text}\n\n"
            f"Validation error:\n{first_error}\n\n"
            "Return corrected JSON only. Required schema:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )

    text = _generate(retry_prompt, schema)
    try:
        return schema.model_validate_json(text)
    except ValidationError as second_error:
        raise LLMError(
            f"LLM output failed {schema.__name__} validation after one retry: {second_error}"
        ) from second_error
