"""Tests for bgv/mock_verifier.py: it must match the real interface and cover every verdict."""
import inspect

import pytest

from bgv.mock_verifier import verify
from bgv.models import EducationClaim, JobClaim, ParsedResume, Verdict

FULL = ParsedResume(
    full_name="Priya Sharma",
    jobs=[
        JobClaim(employer="Infosys", title="Lead", start="2021-03", is_current=True, claimed_team_size=15),
        JobClaim(employer="TCS", title="Analyst", start="2017-06", end="2021-02"),
    ],
    education=[EducationClaim(school="IIT Bombay", degree="B.Tech")],
)


@pytest.mark.parametrize(
    "resume",
    [
        FULL,
        ParsedResume(full_name=""),
        ParsedResume(full_name="Solo", jobs=[JobClaim(employer="", title="")]),
    ],
)
def test_every_verdict_appears_for_any_resume(resume):
    report = verify(resume)

    assert {check.verdict for check in report.checks} == set(Verdict)
    assert report.decision == "human_review"
    assert report.candidate


def test_signature_matches_the_agreed_interface():
    parameters = inspect.signature(verify).parameters

    assert list(parameters) == ["resume", "run_id"]
    assert parameters["run_id"].default is None


def test_run_id_is_kept_or_generated():
    assert verify(FULL, run_id="abc").run_id == "abc"
    assert verify(FULL).run_id != verify(FULL).run_id


def test_uses_the_resumes_claims():
    claims = " ".join(check.claim for check in verify(FULL).checks)

    assert "Lead at Infosys, 2021-03 to present" in claims
    assert "Analyst at TCS" in claims
    assert "Led a team of 15 at Infosys" in claims
    assert "B.Tech at IIT Bombay" in claims


def test_everything_fake_is_labelled_mock():
    report = verify(FULL)

    for check in report.checks:
        assert all(reason.startswith("Mock result:") for reason in check.reasons)
        assert all(source.kind.startswith("mock_") for source in check.sources)
        assert all(source.ref.startswith("https://example.com/mock/") for source in check.sources)
    assert report.referees[0].name == "Mock Referee"
