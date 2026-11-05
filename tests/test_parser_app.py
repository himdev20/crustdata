"""Drives the real app.py with the real Verifier. Only Crustdata and the LLM are faked."""
import json
import os
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import bgv.llm
import bgv.verifier
from bgv.models import CheckResult, Referee, Source, Verdict, VerificationReport
from tests.fakes import FakeClient, company, web

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
SAMPLES = sorted(p.name for p in (Path(APP).parent / "samples").glob("*.txt"))

JOBS = {
    "fake_employer.txt": [
        {"employer": "Nexora Infotech Solutions Pvt Ltd", "title": "Senior Software Engineer",
         "start": "2023-04", "end": "2021-01"},
        {"employer": "Infosys", "title": "Software Engineer", "start": "2018-07", "end": "2023-03"},
    ],
    "team_size_tiny_company.txt": [
        {"employer": "MUDRA FINANCIAL SERVICES", "employer_domain": "mudrafinancial.in",
         "title": "Operations Manager", "start": "2019-06", "end": "2023-08", "claimed_team_size": 15},
        {"employer": "HDFC Bank", "title": "Accounts Executive", "start": "2016-05", "end": "2019-05"},
    ],
    "messy_missing_fields.txt": [
        {"employer": "Wipro", "title": "Support Engg", "start": "2019-03", "is_current": True},
        {"employer": "Tech Mahindra", "title": "", "start": "2017-08", "end": "2019-02"},
    ],
}
NAMES = {
    "fake_employer.txt": "ROHAN KULKARNI",
    "team_size_tiny_company.txt": "MEERA JOSHI",
    "messy_missing_fields.txt": "Sunil K",
}


class FakeLLM:
    """Answers the parse prompt with the resume's jobs, and the summary prompt with a sentence."""

    def __init__(self, sample):
        self.sample = sample

    def generate_json(self, prompt, json_schema):
        if "jobs" in json_schema.get("properties", {}):
            return json.dumps({"full_name": NAMES[self.sample], "jobs": JOBS[self.sample]})
        return json.dumps({"summary": "This restates the verdicts."})


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BGV_DB_PATH", str(tmp_path / "runs.sqlite"))
    st.cache_resource.clear()
    yield
    bgv.llm.set_provider(None)
    st.cache_resource.clear()


def start():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    return at


def run_button(at):
    return next(b for b in at.button if b.label == "Run verification")


def html_blocks(at):
    return [element.proto.body for element in at.get("html")]


def text_of(at):
    return "\n".join(element.value for element in at.markdown)


def expander_labels(at):
    return [element.label for element in at.expander]


def stub_verifier(monkeypatch, report, messages=(), error=None):
    """Replace Verifier with one that reports the given progress, then returns or raises."""

    class Stub:
        def __init__(self, store=None, find_refs=True, progress=None):
            self.progress = progress

        def run(self, source, filename=None):
            for message in messages:
                self.progress(message)
            if error:
                raise error
            return report

    monkeypatch.setattr(bgv.verifier, "Verifier", Stub)


def make_report(decision="human_review", checks=None, referees=()):
    return VerificationReport(
        run_id="run1",
        candidate="Priya Sharma",
        decision=decision,
        summary="Plain summary.",
        checks=checks if checks is not None else [
            CheckResult(claim="Lead at Infosys, 2021-03 to present", check="tenure", verdict=Verdict.VERIFIED,
                        reasons=["Dates match."])
        ],
        referees=list(referees),
    )


def verify_pasted(at):
    at.text_area(key="resume_text").set_value("Some resume text\nSecond line").run()
    run_button(at).click().run()
    assert not at.exception


# ---------- Load sample ----------

def test_dropdown_lists_every_sample_file():
    at = start()

    assert at.selectbox(key="sample_choice").options == SAMPLES
    assert SAMPLES, "samples/ should not be empty"


@pytest.mark.parametrize("sample", SAMPLES)
def test_one_selection_fills_the_text_box_and_enables_verify(sample):
    at = start()
    assert run_button(at).disabled

    at.selectbox(key="sample_choice").select(sample).run()

    assert at.text_area(key="resume_text").value == (Path(APP).parent / "samples" / sample).read_text()
    assert not run_button(at).disabled


# ---------- Every sample through the real verifier ----------

@pytest.mark.parametrize("sample", SAMPLES)
def test_each_sample_renders_a_full_report(monkeypatch, sample):
    bgv.llm.set_provider(FakeLLM(sample))
    client = FakeClient(
        companies=[company(1, "Infosys", "infosys.com", 1981, 300000), company(2, "Wipro", "wipro.com", 1945, 200000)],
        web_results={"Infosys": [web("Infosys", "https://www.infosys.com", "Infosys company")]},
    )
    monkeypatch.setattr(bgv.verifier, "CrustdataClient", lambda: client)
    at = start()
    at.selectbox(key="sample_choice").select(sample).run()

    run_button(at).click().run()

    assert not at.exception
    assert not at.error
    report = at.session_state["report"]
    assert report.candidate == NAMES[sample]
    assert any(report.decision.replace("human_review", "Human review") in block for block in html_blocks(at))
    assert "Evidence" in expander_labels(at)
    page = text_of(at).replace("\\", "")
    for job in JOBS[sample]:
        assert f"#### {job['employer']}" in page
    assert "This restates the verdicts." in page
    assert sum(metric.value.isdigit() and int(metric.value) for metric in at.metric[:4]) == len(report.checks)


# ---------- Banner and tiles ----------

def test_auto_clear_banner_is_green(monkeypatch):
    stub_verifier(monkeypatch, make_report(decision="auto_clear"))
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    banner = html_blocks(at)[0]
    assert "Auto-clear" in banner and "#1a7f37" in banner


def test_human_review_banner_is_amber_and_counts_flagged_checks(monkeypatch):
    checks = [
        CheckResult(claim="c", check="tenure", verdict=verdict, reasons=["r"])
        for verdict in (Verdict.VERIFIED, Verdict.DISCREPANCY, Verdict.CONTRADICTED, Verdict.CANNOT_VERIFY,
                        Verdict.CANNOT_VERIFY)
    ]
    stub_verifier(monkeypatch, make_report(checks=checks))
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    banner = html_blocks(at)[0]
    assert "Human review" in banner and "#f5a623" in banner
    assert "4 of 5 checks" in banner
    assert [(m.label, m.value) for m in at.metric[:4]] == [
        ("✅ Verified", "1"), ("⚠️ Discrepancy", "1"), ("❌ Contradicted", "1"), ("❓ Cannot verify", "2"),
    ]


def test_candidate_name_is_escaped_in_the_banner(monkeypatch):
    report = make_report().model_copy(update={"candidate": "<img src=x onerror=alert(1)>"})
    stub_verifier(monkeypatch, report)
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    assert "<img" not in html_blocks(at)[0]


# ---------- Checks and evidence ----------

def test_evidence_links_are_clickable_and_only_for_http(monkeypatch):
    check = CheckResult(
        claim="c",
        check="employer_exists",
        verdict=Verdict.VERIFIED,
        reasons=["Found it."],
        sources=[
            Source(kind="web_search", ref="Acme site -> https://acme.example/about", retrieved_at="t"),
            Source(kind="crustdata_company_identify", ref="/company/identify 'Acme'", retrieved_at="t"),
            Source(kind="web_search", ref="javascript:alert(1)", retrieved_at="t"),
        ],
    )
    stub_verifier(monkeypatch, make_report(checks=[check]))
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    page = text_of(at)
    assert "(https://acme.example/about)" in page
    assert "](javascript" not in page
    assert "Crustdata company lookup" in page


def test_resume_text_in_a_reason_cannot_add_links(monkeypatch):
    check = CheckResult(claim="c", check="tenure", verdict=Verdict.DISCREPANCY,
                        reasons=["[click](https://evil.example)"])
    stub_verifier(monkeypatch, make_report(checks=[check]))
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    assert "](https://evil.example)" not in text_of(at)


# ---------- Referees ----------

def test_referee_draft_is_in_a_labelled_box(monkeypatch):
    referee = Referee(name="Asha Rao", profile_url="https://linkedin.com/in/asha", overlap="2021 at Infosys",
                      draft_message="Hello Asha, could you confirm this?")
    stub_verifier(monkeypatch, make_report(referees=[referee]))
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    assert "Draft — not sent" in text_of(at)
    assert [element.value for element in at.code] == ["Hello Asha, could you confirm this?"]
    assert "[Asha Rao](https://linkedin.com/in/asha)" in text_of(at)


# ---------- Progress ----------

def stage_text(at):
    return next(element.value for element in at.markdown if "Reading resume" in element.value)


def test_stages_all_finish_and_jumped_stages_show_skipped(monkeypatch):
    stub_verifier(monkeypatch, make_report(), messages=["parse: running", "person: loaded from checkpoint"])
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    assert stage_text(at).split("  \n") == [
        "✅ Reading resume",
        "➖ Checking employers (skipped)",
        "✅ Checking career history",
        "➖ Finding referees (skipped)",
        "✅ Writing report",
    ]


def test_failure_marks_the_running_stage_and_shows_an_error(monkeypatch):
    stub_verifier(monkeypatch, None, messages=["parse: running", "company:1: running"],
                  error=RuntimeError("upstream down"))
    at = start()
    verify_pasted(at)

    assert "❌ Checking employers (job 2)" in stage_text(at)
    assert "upstream down" in at.error[0].value
    assert "report" not in at.session_state


def test_unfamiliar_progress_messages_are_ignored(monkeypatch):
    stub_verifier(monkeypatch, make_report(), messages=["something new happened"])
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    at = start()
    verify_pasted(at)

    assert not at.error
    assert "report" in at.session_state


# ---------- Summary cache ----------

def test_same_run_with_new_results_gets_a_new_summary(monkeypatch):
    answers = iter(["First summary.", "Second summary."])

    class Provider:
        def generate_json(self, prompt, json_schema):
            return json.dumps({"summary": next(answers)})

    bgv.llm.set_provider(Provider())
    at = start()
    at.session_state["report"] = make_report()
    at.run()
    assert "First summary." in text_of(at)
    at.run()
    assert "First summary." in text_of(at)  # a rerun must not call the LLM again

    changed = make_report(checks=[CheckResult(claim="c", check="title", verdict=Verdict.DISCREPANCY, reasons=["r"])])
    at.session_state["report"] = changed
    at.run()
    assert "Second summary." in text_of(at)


# ---------- Review queue and downloads ----------

def test_queue_approve_and_downloads_still_work(monkeypatch):
    bgv.llm.set_provider(FakeLLM(SAMPLES[0]))
    sample = SAMPLES[0]
    client = FakeClient(companies=[company(1, "Infosys", "infosys.com", 1981, 300000)])
    monkeypatch.setattr(bgv.verifier, "CrustdataClient", lambda: client)
    at = start()
    at.selectbox(key="sample_choice").select(sample).run()
    run_button(at).click().run()
    assert not at.exception
    run_id = at.session_state["report"].run_id

    downloads = [element.proto.label for element in at.get("download_button")]
    # One pair on the verify tab and one on the queue tab.
    assert downloads.count("Download Markdown") == 2
    assert downloads.count("Download HTML") == 2

    at.text_area(key=f"notes-{run_id}").set_value("Checked by phone.").run()
    at.button(key=f"ok-{run_id}").click().run()
    assert not at.exception

    from bgv.store import Store

    saved = Store().get_run(run_id)
    assert (saved["review_status"], saved["reviewer_notes"]) == ("approved", "Checked by phone.")
    assert any("Nothing waiting" in element.value for element in at.info)
