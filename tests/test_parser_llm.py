"""Offline tests for bgv/llm.py. A fake client stands in for Gemini; the errors are the SDK's own."""
from types import SimpleNamespace

import pytest
from google.genai import errors
from pydantic import BaseModel

from bgv import llm
from bgv.models import ParsedResume


class Answer(BaseModel):
    value: int


def rate_limit_error():
    return errors.ClientError(
        429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}
    )


class FakeClient:
    """Plays back a script: strings are response text, exceptions are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(text=step)


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(llm.time, "sleep", recorded.append)
    return recorded


def use_script(monkeypatch, *script):
    fake = FakeClient(script)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    return fake


def test_valid_json_returns_instance_and_requests_json(monkeypatch, sleeps):
    fake = use_script(monkeypatch, '{"value": 7}')

    assert llm.complete_json("q", Answer) == Answer(value=7)
    assert len(fake.calls) == 1
    assert fake.calls[0].config.response_mime_type == "application/json"
    assert fake.calls[0].config.response_json_schema == Answer.model_json_schema()


def test_invalid_output_retries_once_with_the_validation_error(monkeypatch, sleeps):
    fake = use_script(monkeypatch, '{"value": "oops"}', '{"value": 7}')

    assert llm.complete_json("q", Answer).value == 7
    assert len(fake.calls) == 2
    retry_prompt = fake.calls[1].contents
    assert retry_prompt.startswith("q")
    assert '{"value": "oops"}' in retry_prompt
    assert "value" in retry_prompt and "valid integer" in retry_prompt


def test_second_invalid_output_raises_and_stops(monkeypatch, sleeps):
    fake = use_script(monkeypatch, "not json", "still not json", '{"value": 7}')

    with pytest.raises(llm.LLMError, match="after one retry"):
        llm.complete_json("q", Answer)
    assert len(fake.calls) == 2


def test_empty_response_takes_the_validation_retry(monkeypatch, sleeps):
    fake = use_script(monkeypatch, None, '{"value": 7}')

    assert llm.complete_json("q", Answer).value == 7
    assert len(fake.calls) == 2


def test_429_backs_off_with_growing_delays_then_succeeds(monkeypatch, sleeps):
    fake = use_script(monkeypatch, rate_limit_error(), rate_limit_error(), '{"value": 7}')

    assert llm.complete_json("q", Answer).value == 7
    assert len(fake.calls) == 3
    assert len(sleeps) == 2
    assert 2 <= sleeps[0] < 3
    assert 4 <= sleeps[1] < 5


def test_429_gives_up_after_max_retries(monkeypatch, sleeps):
    fake = use_script(monkeypatch, *[rate_limit_error() for _ in range(10)])

    with pytest.raises(llm.LLMError, match="rate limited"):
        llm.complete_json("q", Answer)
    assert len(fake.calls) == llm.MAX_RATE_LIMIT_RETRIES + 1
    assert len(sleeps) == llm.MAX_RATE_LIMIT_RETRIES


def test_other_api_errors_fail_without_retry(monkeypatch, sleeps):
    bad_request = errors.ClientError(
        400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad schema"}}
    )
    fake = use_script(monkeypatch, bad_request)

    with pytest.raises(llm.LLMError, match="400"):
        llm.complete_json("q", Answer)
    assert len(fake.calls) == 1
    assert sleeps == []


def test_missing_api_key_gives_a_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm._client.cache_clear()

    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY is not set"):
        llm.complete_json("q", Answer)


def test_real_sdk_accepts_the_parsed_resume_schema():
    """Builds the real request config, so a schema the SDK rejects fails here."""
    from google.genai import types

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=ParsedResume.model_json_schema(),
    )
    assert "jobs" in config.response_json_schema["properties"]
