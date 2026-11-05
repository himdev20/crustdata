"""Tests for bgv/report.py. The LLM is scripted; the rendering is real."""
import json

import pytest

from bgv import report as report_module
from bgv.llm import LLMError
from bgv.models import CheckResult, Referee, Source, Verdict, VerificationReport
from bgv.report import to_html, to_markdown, with_summary


def source(ref="https://example.com/a", kind="crustdata_company_enrich"):
    return Source(kind=kind, ref=ref, retrieved_at="2026-11-06T00:00:00+00:00")


def make_report(**changes):
    values = dict(
        run_id="run1",
        candidate="Priya Sharma",
        summary="All checks are listed below.",
        decision="human_review",
        checks=[
            CheckResult(
                claim=f"Claim for {verdict.value}",
                check="tenure",
                verdict=verdict,
                reasons=[f"Reason for {verdict.value}"],
                sources=[source()],
            )
            for verdict in Verdict
        ],
        referees=[
            Referee(
                name="Asha Rao",
                profile_url="https://linkedin.com/in/asha",
                title_at_employer="Manager",
                overlap="2021-05 to 2022-11 at Infosys",
                draft_message="Hello Asha,\nCould you confirm this?",
            )
        ],
    )
    return VerificationReport(**{**values, **changes})


@pytest.mark.parametrize("render", [to_markdown, to_html])
def test_sections_appear_in_the_requested_order(render):
    output = render(make_report())

    positions = [
        output.index(text)
        for text in ("Priya Sharma", "All checks are listed", "Claim for verified", "Asha Rao", "Human review required")
    ]
    assert positions == sorted(positions)


@pytest.mark.parametrize("render", [to_markdown, to_html])
def test_every_verdict_has_its_symbol_and_a_row(render):
    output = render(make_report())

    for symbol in ("✅", "⚠️", "❌", "❓"):
        assert symbol in output
    for verdict in Verdict:
        assert f"Claim for {verdict.value}" in output.replace("\\_", "_")


@pytest.mark.parametrize("render", [to_markdown, to_html])
def test_referee_drafts_are_labelled_not_sent(render):
    output = render(make_report())

    assert "Draft — not sent" in output
    assert output.index("Draft — not sent") < output.index("Could you confirm")


def test_markdown_sources_are_clickable():
    assert "(https://example.com/a)" in to_markdown(make_report())


def test_html_sources_are_clickable():
    assert 'href="https://example.com/a"' in to_html(make_report())


@pytest.mark.parametrize("ref", ["javascript:alert(1)", "crustdata_company_id=42", "data:text/html,x"])
def test_non_http_sources_are_never_links(ref):
    check = CheckResult(claim="c", check="x", verdict=Verdict.VERIFIED, reasons=[], sources=[source(ref=ref)])
    report = make_report(checks=[check])

    assert f"]({ref})" not in to_markdown(report)
    assert "href=" not in to_html(make_report(checks=[check], referees=[]))


def test_hostile_resume_text_is_escaped_in_html():
    attack = '<script>alert(1)</script>'
    check = CheckResult(claim=attack, check="x", verdict=Verdict.VERIFIED, reasons=[attack])
    output = to_html(make_report(candidate=attack, summary=attack, checks=[check]))

    assert "<script>" not in output
    assert "&lt;script&gt;" in output


def test_a_url_cannot_break_out_of_an_html_attribute():
    check = CheckResult(
        claim="c",
        check="x",
        verdict=Verdict.VERIFIED,
        reasons=[],
        sources=[source(ref='https://example.com/"onclick="alert(1)')],
    )

    assert 'onclick="' not in to_html(make_report(checks=[check], referees=[]))


def test_hostile_text_cannot_break_the_markdown_table_or_add_links():
    check = CheckResult(
        claim="Lead | [click](https://evil.example)\nnew line",
        check="x",
        verdict=Verdict.VERIFIED,
        reasons=[],
    )
    row = next(line for line in to_markdown(make_report(checks=[check])).splitlines() if "Lead" in line)

    assert "\\|" in row
    assert "\\[click\\]" in row
    assert "](https://evil.example)" not in row


def test_empty_report_still_renders():
    report = make_report(checks=[], referees=[], summary="")

    for output in (to_markdown(report), to_html(report)):
        assert "No checks were run." in output
        assert "No referees suggested." in output
        assert "No summary available." in output


def test_decision_labels():
    assert "Auto-clear" in to_markdown(make_report(decision="auto_clear"))
    assert "Human review required" in to_html(make_report(decision="human_review"))


# ---------- Summary ----------

def script_llm(monkeypatch, answer):
    calls = []

    def fake(prompt, schema):
        calls.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return schema(summary=answer)

    monkeypatch.setattr(report_module, "complete_json", fake)
    return calls


def test_summary_makes_one_call_and_only_changes_the_summary(monkeypatch):
    calls = script_llm(monkeypatch, "Priya Sharma has four checks and needs human review.")
    original = make_report()

    result = with_summary(original)

    assert result.summary == "Priya Sharma has four checks and needs human review."
    assert len(calls) == 1
    assert result.checks == original.checks
    assert result.decision == original.decision


def test_summary_prompt_contains_verdicts_but_not_sources_or_referees(monkeypatch):
    calls = script_llm(monkeypatch, "Fine.")

    with_summary(make_report())

    assert "Could you confirm" not in calls[0]
    assert "example.com" not in calls[0]
    facts = json.loads(calls[0].split("<<<DATA\n")[1].split("\nDATA>>>")[0])
    assert facts["decision"] == "human_review"
    assert facts["verdict_counts"]["contradicted"] == 1
    assert {check["verdict"] for check in facts["checks"]} == {verdict.value for verdict in Verdict}


def test_llm_failure_falls_back_to_a_plain_summary(monkeypatch):
    script_llm(monkeypatch, LLMError("quota"))

    summary = with_summary(make_report()).summary

    assert summary == (
        "Priya Sharma: 4 checks run, with 1 verified, 1 discrepancy, 1 contradicted, "
        "1 cannot verify. Decision: Human review required."
    )


@pytest.mark.parametrize("answer", ["", "   ", "She led a team of 99 people from 2004."])
def test_empty_or_invented_number_falls_back(monkeypatch, answer):
    script_llm(monkeypatch, answer)

    assert with_summary(make_report()).summary.startswith("Priya Sharma: 4 checks run")


def test_numbers_from_the_results_are_allowed(monkeypatch):
    check = CheckResult(claim="Led a team of 15 in 2021", check="x", verdict=Verdict.DISCREPANCY, reasons=[])
    script_llm(monkeypatch, "The claim of a team of 15 in 2021 shows a discrepancy.")

    assert "team of 15" in with_summary(make_report(checks=[check])).summary
