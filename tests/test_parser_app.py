"""Tests for app.py, driving the real Streamlit script. Only the LLM is scripted."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app
from bgv import llm, report, resume_parser
from bgv.models import EducationClaim, JobClaim, ParsedResume

RESUME_TEXT = "Priya Sharma\nLead at Infosys, Mar 2021 - Present. Led a team of 15.\nIIT Bombay, B.Tech"
PARSED = ParsedResume(
    full_name="Priya Sharma",
    jobs=[
        JobClaim(
            employer="Infosys",
            title="Lead",
            start="2021-03",
            is_current=True,
            claimed_team_size=15,
            raw_text="Lead at Infosys, Mar 2021 - Present.",
        )
    ],
    education=[EducationClaim(school="IIT Bombay", degree="B.Tech")],
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Keep the queue in a temp file, use the mock verifier, and script the LLM."""
    monkeypatch.setenv("BGV_REVIEW_FILE", str(tmp_path / "queue.json"))
    monkeypatch.setenv("BGV_VERIFIER", "mock")
    monkeypatch.setattr(resume_parser, "complete_json", lambda prompt, schema: PARSED)

    def no_summary(prompt, schema):
        raise llm.LLMError("no key in tests")

    monkeypatch.setattr(report, "complete_json", no_summary)


def start():
    at = AppTest.from_file(str(Path(app.__file__).resolve()), default_timeout=30)
    at.run()
    assert not at.exception
    return at


def parse_resume_text(at):
    at.text_area(key="resume_text").set_value(RESUME_TEXT)
    at.button(key="parse").click().run()
    assert not at.exception


def errors(at):
    return [element.value for element in at.error]


# ---------- Conversions, which the edit tables depend on ----------

def test_jobs_survive_the_table_round_trip():
    assert app.frame_to_jobs(app.jobs_to_frame(PARSED.jobs)) == PARSED.jobs


def test_education_survives_the_table_round_trip():
    education = [EducationClaim(school="IIT", degree="B.Tech", start_year=2013)]

    assert app.frame_to_education(app.education_to_frame(education)) == education


def test_empty_tables_are_valid():
    assert app.frame_to_jobs(app.jobs_to_frame([])) == []
    assert app.frame_to_education(app.education_to_frame([])) == []


def test_blank_rows_are_dropped_and_blank_cells_become_none():
    frame = app.jobs_to_frame(
        [JobClaim(employer="TCS", title="", start=""), JobClaim(employer="", title="  ")]
    )

    jobs = app.frame_to_jobs(frame)

    assert len(jobs) == 1
    assert jobs[0].start is None


def test_marking_a_job_current_clears_its_end_date():
    frame = app.jobs_to_frame([JobClaim(employer="TCS", title="", end="2020-01")])
    frame.loc[0, "is_current"] = True

    assert app.frame_to_jobs(frame)[0].end is None


# ---------- Verifier setting ----------

def test_one_setting_selects_the_verifier(monkeypatch):
    from bgv import mock_verifier

    assert app.load_verify() is mock_verifier.verify

    monkeypatch.setenv("BGV_VERIFIER", "nonsense")
    with pytest.raises(ValueError, match="BGV_VERIFIER"):
        app.load_verify()


def test_real_setting_targets_the_teammates_module(monkeypatch):
    imported = []
    monkeypatch.setattr(
        app.importlib,
        "import_module",
        lambda name: imported.append(name) or type("Module", (), {"verify": "real"}),
    )
    monkeypatch.setenv("BGV_VERIFIER", "real")

    assert app.load_verify() == "real"
    assert imported == ["bgv.verifier"]


# ---------- Review queue storage ----------

def test_a_recorded_decision_cannot_be_overwritten():
    from bgv.mock_verifier import verify

    app.enqueue_run(verify(PARSED, run_id="r1"), "mock")
    app.record_review("r1", "approved", "Asha")

    with pytest.raises(ValueError, match="already been reviewed"):
        app.record_review("r1", "rejected", "Ravi")
    assert app.load_queue()["r1"]["review"]["decision"] == "approved"


# ---------- Flows through the real script ----------

def test_paste_then_parse_shows_editable_claims():
    at = start()
    parse_resume_text(at)

    assert at.text_input(key="full_name_0").value == "Priya Sharma"
    assert "Check the claims" in at.subheader[0].value
    assert at.warning, "the mock warning should be visible"


def test_empty_input_shows_an_error():
    at = start()
    at.button(key="parse").click().run()

    assert errors(at) == ["Add a resume first."]


def test_parser_failure_is_shown_and_keeps_the_input(monkeypatch):
    def fail(prompt, schema):
        raise llm.LLMError("GEMINI_API_KEY is not set (check your .env file).")

    monkeypatch.setattr(resume_parser, "complete_json", fail)
    at = start()
    parse_resume_text(at)

    assert errors(at) == ["GEMINI_API_KEY is not set (check your .env file)."]
    assert at.text_area(key="resume_text").value == RESUME_TEXT


def test_verifier_failure_is_shown_and_keeps_the_claims(monkeypatch):
    monkeypatch.setenv("BGV_VERIFIER", "nonsense")
    at = start()
    parse_resume_text(at)
    at.button(key="verify").click().run()

    assert "BGV_VERIFIER" in errors(at)[0]
    assert at.text_input(key="full_name_0").value == "Priya Sharma"


def test_full_flow_from_paste_to_decision():
    at = start()
    parse_resume_text(at)
    at.text_input(key="full_name_0").set_value("Priya S Sharma")
    at.button(key="verify").click().run()
    assert not at.exception

    report = at.session_state["report"]
    assert report.candidate == "Priya S Sharma"
    page = " ".join(element.value for element in at.markdown)
    assert "Priya S Sharma" in page
    assert "Draft — not sent" in page

    queue = json.loads(app.queue_path().read_text())
    assert queue[report.run_id]["review"] is None

    at.sidebar.radio(key="page").set_value("Human review queue").run()
    assert not at.exception
    at.button(key=f"approve_{report.run_id}").click().run()
    assert errors(at) == ["Enter your name first."]
    assert app.load_queue()[report.run_id]["review"] is None

    at.text_input(key="reviewer").set_value("Asha").run()
    at.button(key=f"reject_{report.run_id}").click().run()
    assert not at.exception

    review = app.load_queue()[report.run_id]["review"]
    assert (review["decision"], review["reviewer"]) == ("rejected", "Asha")
    assert not [button for button in at.button if button.key == f"approve_{report.run_id}"]
