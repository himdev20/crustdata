"""Offline tests for bgv/resume_parser.py. The LLM is scripted; everything else is real."""
import pytest

from bgv import resume_parser
from bgv.models import EducationClaim, JobClaim, ParsedResume
from bgv.resume_parser import ResumeParseError, parse_resume


def make_pdf(line: str) -> bytes:
    """Build a small real PDF with one line of text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)
    return pdf


@pytest.fixture
def llm_returns(monkeypatch):
    """Script the LLM's answer and record the prompt it was sent."""
    prompts = []

    def script(answer):
        def fake(prompt, schema):
            prompts.append(prompt)
            return answer

        monkeypatch.setattr(resume_parser, "complete_json", fake)
        return prompts

    return script


RESUME = """Priya Sharma | priya@gmail.com | linkedin.com/in/priya-sharma | Pune
Senior Engineer, Infosys (infosys.com) Mar 2021 - Present. Led a team of 15.
Analyst at TCS 2017
IIT Bombay, B.Tech Computer Science, 2013-2017"""


def test_a_good_answer_passes_through_unchanged(llm_returns):
    answer = ParsedResume(
        full_name="Priya Sharma",
        email="priya@gmail.com",
        location="Pune",
        jobs=[
            JobClaim(
                employer="Infosys",
                employer_domain="infosys.com",
                title="Senior Engineer",
                start="2021-03",
                is_current=True,
                claimed_team_size=15,
                raw_text="Senior Engineer, Infosys (infosys.com) Mar 2021 - Present. Led a team of 15.",
            )
        ],
        education=[
            EducationClaim(
                school="IIT Bombay",
                degree="B.Tech",
                field="Computer Science",
                start_year=2013,
                end_year=2017,
            )
        ],
    )
    llm_returns(answer)

    assert parse_resume(RESUME, is_pdf=False) == answer


def test_the_resume_reaches_the_prompt_as_marked_data(llm_returns):
    prompts = llm_returns(ParsedResume(full_name=""))

    parse_resume(RESUME, is_pdf=False)

    assert f"<<<RESUME\n{RESUME}\nRESUME>>>" in prompts[0]


def test_invented_job_and_school_are_dropped(llm_returns):
    llm_returns(
        ParsedResume(
            full_name="Priya Sharma",
            jobs=[
                JobClaim(employer="Google", title="Engineer"),
                JobClaim(employer="TCS", title="Analyst"),
            ],
            education=[EducationClaim(school="Stanford University")],
        )
    )

    result = parse_resume(RESUME, is_pdf=False)

    assert [job.employer for job in result.jobs] == ["TCS"]
    assert result.education == []


def test_invented_optional_values_become_none(llm_returns):
    llm_returns(
        ParsedResume(
            full_name="Rahul Verma",
            email="rahul@tcs.com",
            location="Bengaluru",
            linkedin_url="https://linkedin.com/in/made-up",
            jobs=[
                JobClaim(
                    employer="TCS",
                    title="Director",
                    employer_domain="tcs.com",
                    raw_text="Director at TCS since 2010",
                )
            ],
            education=[EducationClaim(school="IIT Bombay", degree="MBA", field="Finance")],
        )
    )

    result = parse_resume(RESUME, is_pdf=False)

    assert result.full_name == ""
    assert (result.email, result.location, result.linkedin_url) == (None, None, None)
    job = result.jobs[0]
    assert (job.title, job.employer_domain, job.raw_text) == ("", None, "")
    assert (result.education[0].degree, result.education[0].field) == (None, None)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2021-03", "2021-03"),
        ("2021", None),  # no month: never guess one
        ("Mar 2021", None),  # not normalised
        ("2021-13", None),  # no such month
        ("2099-01", None),  # the year isn't in the resume
        ("", None),
    ],
)
def test_only_valid_dates_the_resume_shows_survive(llm_returns, value, expected):
    llm_returns(ParsedResume(full_name="", jobs=[JobClaim(employer="TCS", title="", start=value)]))

    assert parse_resume(RESUME, is_pdf=False).jobs[0].start == expected


def test_a_two_digit_year_on_the_resume_counts_as_shown(llm_returns):
    llm_returns(ParsedResume(full_name="", jobs=[JobClaim(employer="TCS", title="", start="2021-03")]))

    assert parse_resume("TCS, Mar '21 onwards", is_pdf=False).jobs[0].start == "2021-03"


def test_matching_ignores_punctuation_but_respects_word_boundaries(llm_returns):
    llm_returns(
        ParsedResume(
            full_name="",
            education=[
                EducationClaim(school="IIT Bombay", degree="BTech"),
                EducationClaim(school="IIT Bombay", degree="MBA"),
            ],
        )
    )

    result = parse_resume("IIT Bombay, B.Tech", is_pdf=False)

    assert [item.degree for item in result.education] == ["BTech", None]


def test_digits_inside_a_longer_number_do_not_count_as_a_year(llm_returns):
    llm_returns(ParsedResume(full_name="", jobs=[JobClaim(employer="TCS", title="", start="2021-03")]))

    assert parse_resume("TCS. Phone 9821217654", is_pdf=False).jobs[0].start is None


def test_impossible_dates_are_kept_for_the_verifier(llm_returns):
    text = "Nexora Infotech Solutions Pvt Ltd, 03/2023 to 01/2019"
    llm_returns(
        ParsedResume(
            full_name="",
            jobs=[JobClaim(employer="Nexora Infotech Solutions Pvt Ltd", title="", start="2023-03", end="2019-01")],
        )
    )

    job = parse_resume(text, is_pdf=False).jobs[0]

    assert (job.start, job.end) == ("2023-03", "2019-01")


@pytest.mark.parametrize("end", ["Present", "present", "Till Date", "Current"])
def test_present_as_an_end_date_means_current(llm_returns, end):
    llm_returns(ParsedResume(full_name="", jobs=[JobClaim(employer="Infosys", title="", end=end)]))

    job = parse_resume(RESUME, is_pdf=False).jobs[0]

    assert job.is_current is True
    assert job.end is None


def test_a_current_job_never_keeps_an_end_date(llm_returns):
    llm_returns(
        ParsedResume(
            full_name="",
            jobs=[JobClaim(employer="Infosys", title="", end="2021-03", is_current=True)],
        )
    )

    assert parse_resume(RESUME, is_pdf=False).jobs[0].end is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("infosys.com", "infosys.com"),
        ("https://www.infosys.com/about", "infosys.com"),
        ("hr@infosys.com", "infosys.com"),
        ("gmail.com", None),  # personal mailbox, not the employer
        ("wipro.com", None),  # not in the resume
        ("Infosys", None),  # not a domain
    ],
)
def test_employer_domain_cleaning(llm_returns, value, expected):
    llm_returns(
        ParsedResume(full_name="", jobs=[JobClaim(employer="Infosys", title="", employer_domain=value)])
    )

    assert parse_resume(RESUME, is_pdf=False).jobs[0].employer_domain == expected


def test_linkedin_url_gets_a_scheme_and_must_be_linkedin(llm_returns):
    llm_returns(ParsedResume(full_name="", linkedin_url="linkedin.com/in/priya-sharma"))
    assert parse_resume(RESUME, is_pdf=False).linkedin_url == "https://linkedin.com/in/priya-sharma"

    llm_returns(ParsedResume(full_name="", linkedin_url="infosys.com"))
    assert parse_resume(RESUME, is_pdf=False).linkedin_url is None


def test_non_positive_team_size_is_removed(llm_returns):
    llm_returns(
        ParsedResume(full_name="", jobs=[JobClaim(employer="TCS", title="", claimed_team_size=0)])
    )

    assert parse_resume(RESUME, is_pdf=False).jobs[0].claimed_team_size is None


def test_pdf_text_is_extracted_and_sent_to_the_llm(llm_returns):
    prompts = llm_returns(ParsedResume(full_name="Priya Sharma"))

    result = parse_resume(make_pdf("Priya Sharma, Engineer at Infosys"), is_pdf=True)

    assert "Priya Sharma, Engineer at Infosys" in prompts[0]
    assert result.full_name == "Priya Sharma"


def test_text_bytes_are_decoded(llm_returns):
    prompts = llm_returns(ParsedResume(full_name=""))

    parse_resume("Priya Sharma".encode(), is_pdf=False)

    assert "Priya Sharma" in prompts[0]


@pytest.mark.parametrize(
    "source, is_pdf, message",
    [
        ("   \n ", False, "No text found"),
        (b"this is not a pdf", True, "Could not read"),
        ("not bytes", True, "must be passed as bytes"),
    ],
)
def test_unreadable_input_raises_without_calling_the_llm(monkeypatch, source, is_pdf, message):
    def fail(prompt, schema):
        raise AssertionError("LLM should not be called")

    monkeypatch.setattr(resume_parser, "complete_json", fail)

    with pytest.raises(ResumeParseError, match=message):
        parse_resume(source, is_pdf=is_pdf)
