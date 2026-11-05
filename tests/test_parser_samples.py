"""The samples must contain what they exist to test, and run through the pipeline offline."""
from pathlib import Path

import pytest

from bgv.mock_verifier import verify
from bgv.models import ParsedResume, Verdict
from bgv.report import to_markdown
from bgv.resume_parser import _extract_text

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("fake_employer.txt", ["Nexora Infotech Solutions Pvt Ltd", "April 2023 - January 2021"]),
        ("team_size_tiny_company.txt", ["Led a team of 15", "mudrafinancial.in"]),
        ("messy_missing_fields.txt", ["till date", "Feb '19", "Tech Mahindra"]),
    ],
)
def test_sample_contains_its_test_case(name, expected):
    text = _extract_text((SAMPLES / name).read_text(), is_pdf=False)

    for snippet in expected:
        assert snippet in text


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.txt")), ids=lambda path: path.name)
def test_no_sample_has_a_linkedin_url_for_a_fictional_person(path):
    assert "linkedin.com" not in path.read_text().casefold()


def test_mock_report_renders_for_a_minimal_resume():
    report = verify(ParsedResume(full_name="Sunil K"))

    assert {check.verdict for check in report.checks} == set(Verdict)
    assert "Sunil K" in to_markdown(report)
