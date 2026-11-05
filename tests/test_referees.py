import inspect

from bgv import referees as referees_module
from bgv.company_checks import CompanyResult
from bgv.models import JobClaim, ParsedResume
from bgv.person_checks import check_person
from bgv.referees import find_referees, overlap
from tests.fakes import FakeClient
from tests.test_person_checks import AARAV, URL, emp, person

COMPANIES = {0: CompanyResult(job_index=0, employer="Zoho Corporation Pvt Ltd", crustdata_company_id=1032449,
                              matched_name="Zoho")}
RESUME = ParsedResume(full_name="Aarav Sharma", linkedin_url=URL, jobs=[
    JobClaim(employer="Zoho Corporation Pvt Ltd", title="Software Engineer", start="2019-07", end="2021-12")])

MANAGER = person("Priya Iyer", past=[emp("Zoho", "Engineering Manager", "2017-01", "2022-06", cid=1032449)],
                 url="https://www.linkedin.com/in/priya")
PEER = person("Karthik R", current=[emp("Zoho", "Software Engineer", "2020-01", cid=1032449)],
              url="https://www.linkedin.com/in/karthik")
TOO_SHORT = person("Meera N", past=[emp("Zoho", "Intern", "2021-11", "2022-02", cid=1032449)])
BEFORE = person("Old Timer", past=[emp("Zoho", "Engineer", "2010-01", "2015-01", cid=1032449)])
NAMESAKE_CO = person("Other Zoho", past=[emp("Zoho Health", "Nurse", "2019-01", "2022-01", cid=555)])


def test_overlap():
    assert overlap(((2019, 7), (2021, 12)), ((2020, 1), (2026, 11)))[1] == 23
    assert overlap(((2019, 7), (2021, 12)), ((2010, 1), (2015, 1)))[0] is None


def test_finds_overlapping_colleagues_and_excludes_candidate():
    fake = FakeClient(people={URL: AARAV}, search_results=[AARAV, BEFORE, PEER, TOO_SHORT, NAMESAKE_CO, MANAGER])
    p = check_person(fake, RESUME, COMPANIES)
    refs, notes = find_referees(fake, RESUME, COMPANIES, p)
    assert [r.name for r in refs] == ["Priya Iyer", "Karthik R"]  # senior first; candidate/short/non-overlap dropped
    assert refs[0].title_at_employer == "Engineering Manager"
    assert "2019-06 to 2021-12" in refs[0].overlap and "candidate dates from profile" in refs[0].overlap
    assert "Priya" in refs[0].draft_message and "Aarav Sharma" in refs[0].draft_message
    assert fake.searches[-1] == {"field": "experience.employment_details.company_name", "type": "(.)", "value": "Zoho"}
    assert notes == []


def test_uses_resume_dates_when_no_profile():
    fake = FakeClient(search_results=[PEER])
    refs, _ = find_referees(fake, RESUME, COMPANIES, person=None)
    assert refs and "candidate dates from resume" in refs[0].overlap


def test_api_failure_is_reported_not_raised():
    refs, notes = find_referees(FakeClient(fail={"search_people"}), RESUME, COMPANIES, None)
    assert refs == [] and "referee search failed" in notes[0] and "503" in notes[0]


def test_module_cannot_send_messages():
    src = inspect.getsource(referees_module)
    for word in ("smtplib", "requests.post", "send_message", "sendmail", "twilio"):
        assert word not in src
