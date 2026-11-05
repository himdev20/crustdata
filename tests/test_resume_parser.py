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
    text = ("Aarav Sharma\nlinkedin.com/in/aarav-sharma\naarav@example.com\n"
            "Senior Software Engineer, Freshworks, Jan 2022 - Present\n"
            "Software Engineer, Zoho Corporation Pvt Ltd (www.zoho.com), Jul 2019 - Dec 2021\n")
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


# ---------- grounding: values the resume text does not contain are dropped ----------

from bgv.resume_parser import _appears, _year_appears  # noqa: E402

GROUND_TEXT = """Priya Nair
linkedin.com/in/priya-nair-91
Data Analyst - Mu Sigma (mu-sigma.com)        Mar '19 - 2021
B.Tech, IIT Bombay, 2014 - 2018
"""


@pytest.mark.parametrize("value,source,expected", [
    ("MBA", "iit bombay", False),             # not inside another word
    ("Bombay", "iit bombay, 2018", True),
    ("B.Tech", "b tech, iit", True),          # punctuation/spacing may differ
    ("B.Tech", "btech from iit", True),
    ("Tech", "btech from iit", False),
    ("Mu Sigma", "data analyst - mu sigma (mu-sigma.com)", True),
    ("Infosys", "infosystems ltd", False),
    ("", "anything", False),
])
def test_appears_matches_whole_words(value, source, expected):
    assert _appears(value, source) is expected


@pytest.mark.parametrize("year,source,expected", [
    (2019, "mar '19 - 2021", True), (2019, "03/19", True), (2021, "mar '19 - 2021", True),
    (2020, "mar '19 - 2021", False), (2019, "id 120190", False),
])
def test_year_appears(year, source, expected):
    assert _year_appears(year, source) is expected


def ground(payload):
    llm.set_provider(FakeLLM(payload))
    return parse_resume(GROUND_TEXT)


def test_grounding_keeps_values_present_in_the_resume():
    r = ground({
        "full_name": "Priya Nair", "linkedin_url": "https://www.linkedin.com/in/priya-nair-91",
        "jobs": [{"employer": "Mu Sigma", "employer_domain": "mu-sigma.com", "title": "Data Analyst",
                  "start": "2019-03", "end": "2021"}],
        "education": [{"school": "IIT Bombay", "degree": "B.Tech", "start_year": 2014, "end_year": 2018}],
    })
    j = r.jobs[0]
    assert (j.employer, j.employer_domain, j.title, j.start, j.end) == \
        ("Mu Sigma", "mu-sigma.com", "Data Analyst", "2019-03", "2021")
    assert r.education[0].school == "IIT Bombay" and r.education[0].end_year == 2018
    assert r.linkedin_url == "https://www.linkedin.com/in/priya-nair-91"


def test_grounding_drops_invented_values():
    r = ground({
        "full_name": "Priya Nair", "linkedin_url": "https://www.linkedin.com/in/priya-nair-official",
        "jobs": [
            {"employer": "Mu Sigma", "employer_domain": "musigma.ai", "title": "Senior Data Scientist",
             "start": "2017-03", "end": "2021-06"},
            {"employer": "McKinsey & Company", "title": "Consultant", "start": "2021-07"},  # invented job
        ],
        "education": [
            {"school": "IIT Bombay", "degree": "B.Tech", "start_year": 2013, "end_year": 2018},
            {"school": "IIM Ahmedabad", "degree": "MBA", "end_year": 2020},           # invented school
        ],
    })
    assert [j.employer for j in r.jobs] == ["Mu Sigma"]
    j = r.jobs[0]
    assert j.employer_domain is None   # musigma.ai not in the text
    assert j.title == ""               # title not in the text
    assert j.start is None             # 2017 not in the text
    assert j.end == "2021-06"          # 2021 is in the text
    assert [e.school for e in r.education] == ["IIT Bombay"]
    assert (r.education[0].start_year, r.education[0].end_year) == (None, 2018)
    # LLM's invented URL is dropped, but the real one is recovered from the text
    assert r.linkedin_url == "https://www.linkedin.com/in/priya-nair-91"


def test_grounding_drops_linkedin_when_resume_has_none():
    llm.set_provider(FakeLLM({"full_name": "Priya Nair", "linkedin_url": "linkedin.com/in/priya-nair"}))
    r = parse_resume("Priya Nair\nData Analyst - Mu Sigma, 2019 - 2021\nNo profile links here.")
    assert r.linkedin_url is None


def test_mba_is_not_grounded_by_bombay():
    llm.set_provider(FakeLLM({"full_name": "Priya Nair", "jobs": [], "education": [
        {"school": "MBA", "degree": None}]}))
    assert parse_resume(GROUND_TEXT).education == []
