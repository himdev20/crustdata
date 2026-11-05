import pytest
from pydantic import BaseModel

from bgv import llm


class City(BaseModel):
    city: str
    population_millions: float


class FakeProvider:
    """Returns queued answers and records the prompts it saw."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def generate_json(self, prompt, json_schema):
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def reset_provider():
    yield
    llm.set_provider(None)


def test_returns_validated_model():
    llm.set_provider(FakeProvider(['{"city": "Pune", "population_millions": 7.4}']))
    out = llm.complete_json("x", City)
    assert out == City(city="Pune", population_millions=7.4)


def test_accepts_dict_schema_and_code_fences():
    llm.set_provider(FakeProvider(['```json\n{"a": 1}\n```']))
    assert llm.complete_json("x", {"type": "object"}) == {"a": 1}


def test_retries_once_with_error_feedback():
    fake = FakeProvider(['{"city": "Pune"}', '{"city": "Pune", "population_millions": 7}'])
    llm.set_provider(fake)
    assert llm.complete_json("x", City).population_millions == 7
    assert "previous answer was invalid" in fake.prompts[1]


def test_raises_after_retries():
    llm.set_provider(FakeProvider(["not json", "still not json"]))
    with pytest.raises(llm.LLMError):
        llm.complete_json("x", City)


def test_provider_errors_become_llm_error():
    class Broken:
        def generate_json(self, prompt, json_schema):
            raise ConnectionError("boom")

    llm.set_provider(Broken())
    with pytest.raises(llm.LLMError, match="boom"):
        llm.complete_json("x", City)
