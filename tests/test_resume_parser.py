import json

import pytest

from bgv import llm
from bgv.resume_parser import (ResumeParseError, extract_text, normalize_linkedin,
                               normalize_month, parse_resume)
from tests.pdf_util import make_pdf


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompt = None

    def generate_json(self, prompt, json_schema):
        self.prompt = prompt
        return json.dumps(self.payload)


@pytest.fixture(autouse=True)
def reset():
    yield
    llm.set_provider(None)


@pytest.mark.parametrize("raw,expected", [
    ("2021-03", "2021-03"), ("2021-3", "2021-03"), ("Mar 2021", "2021-03"), ("March, 2021", "2021-03"),
    ("03/2021", "2021-03"), ("2021-03-15", "2021-03"), ("2021", "2021"), ("Sept 2020", "2020-09"),
    ("Present", None), ("", None), (None, None), ("2021-13", None), ("soon", None),
])
def test_normalize_month(raw, expected):
    assert normalize_month(raw) == expected


def test_normalize_linkedin():
    assert normalize_linkedin("linkedin.com/in/aarav-sharma/") == "https://www.linkedin.com/in/aarav-sharma"
    assert normalize_linkedin("see https://in.linkedin.com/in/a_b-1 now") == "https://www.linkedin.com/in/a_b-1"
    assert normalize_linkedin("https://linkedin.com/company/acme") is None


def test_extract_text_from_pdf_includes_hidden_links(tmp_path):
    pdf = make_pdf(["Aarav Sharma", "Software Engineer, Zoho  Jul 2019 - Dec 2021"],
                   link="https://www.linkedin.com/in/aarav-sharma-demo")
    path = tmp_path / "cv.pdf"
    path.write_bytes(pdf)
    for text in (extract_text(path), extract_text(str(path)), extract_text(pdf)):
        assert "Aarav Sharma" in text and "Zoho" in text
        assert "linkedin.com/in/aarav-sharma-demo" in text


def test_blank_pdf_is_rejected():
    with pytest.raises(ResumeParseError, match="no extractable text"):
        extract_text(make_pdf([]))


def test_parse_cleans_llm_output():
    fake = FakeLLM({
        "full_name": "Aarav Sharma",
        "linkedin_url": None,
        "jobs": [
            {"employer": " Freshworks ", "title": "Senior Software Engineer", "start": "Jan 2022",
             "end": "2024-01", "is_current": True, "claimed_team_size": 6, "raw_text": "..."},
            {"employer": "Zoho Corporation Pvt Ltd", "employer_domain": "https://www.zoho.com/",
             "title": "Software Engineer", "start": "2019-07", "end": "Dec 2021", "claimed_team_size": 0},
        ],
    })
    llm.set_provider(fake)
    text = "Aarav Sharma\nlinkedin.com/in/aarav-sharma\naarav@example.com\n" + "x" * 50
    r = parse_resume(text)
    assert "Aarav Sharma" in fake.prompt
    assert r.linkedin_url == "https://www.linkedin.com/in/aarav-sharma"  # regex fallback
    assert r.email == "aarav@example.com"
    j1, j2 = r.jobs
    assert (j1.employer, j1.start, j1.end) == ("Freshworks", "2022-01", None)  # current -> no end
    assert (j2.end, j2.employer_domain, j2.claimed_team_size) == ("2021-12", "zoho.com", None)


def test_invalid_llm_output_raises():
    llm.set_provider(FakeLLM({"jobs": "not a list"}))
    with pytest.raises(llm.LLMError):
        parse_resume("Some resume text that is long enough to parse.")
