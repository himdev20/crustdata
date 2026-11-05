from bgv.company_checks import CompanyResult
from bgv.models import JobClaim, ParsedResume, Verdict
from bgv.person_checks import check_person
from tests.fakes import FakeClient

URL = "https://www.linkedin.com/in/aarav-sharma"


def emp(name, title, start, end=None, cid=None):
    return {"name": name, "title": title, "start_date": f"{start}-01T00:00:00" if start else None,
            "end_date": f"{end}-01T00:00:00" if end else None, "crustdata_company_id": cid}


def person(name="Aarav Sharma", current=(), past=(), url=URL):
    return {"basic_profile": {"name": name},
            "experience": {"employment_details": {"current": list(current), "past": list(past)}},
            "social_handles": {"professional_network_identifier": {"profile_url": url}}}


AARAV = person(current=[emp("Freshworks", "Senior Software Engineer", "2022-02", cid=6042422)],
               past=[emp("Zoho", "Member Technical Staff", "2019-06", "2021-12", cid=1032449)])

COMPANIES = {0: CompanyResult(job_index=0, employer="Freshworks", crustdata_company_id=6042422, matched_name="Freshworks"),
             1: CompanyResult(job_index=1, employer="Zoho Corporation Pvt Ltd", crustdata_company_id=1032449,
                              matched_name="Zoho")}


def resume(linkedin=URL, jobs=None):
    return ParsedResume(full_name="Aarav Sharma", linkedin_url=linkedin, jobs=jobs or [
        JobClaim(employer="Freshworks", title="Senior Software Engineer", start="2022-01", is_current=True),
        JobClaim(employer="Zoho Corporation Pvt Ltd", title="Software Engineer", start="2019-07", end="2021-12"),
    ])


def checks(res, i_claim_substr, check):
    return next(c for c in res.checks if i_claim_substr in c.claim and c.check == check)


def test_enrich_path_verifies_dates_within_3_months_and_flags_title():
    fake = FakeClient(people={URL: AARAV})
    res = check_person(fake, resume(), COMPANIES)
    assert res.method == "person_enrich" and fake.calls == ["enrich_person"]
    assert checks(res, "Freshworks", "tenure").verdict == Verdict.VERIFIED  # 1 month off
    assert checks(res, "Zoho", "tenure").verdict == Verdict.VERIFIED
    assert "crustdata_company_id 1032449" in checks(res, "Zoho", "tenure").reasons[1]
    t = checks(res, "Zoho", "title")
    assert t.verdict == Verdict.DISCREPANCY and "Member Technical Staff" in t.reasons[0]
    assert all(c.sources[0].kind == "crustdata_person_enrich" for c in res.checks)


def test_dates_beyond_3_months_are_discrepancy():
    p = person(past=[emp("Zoho", "Software Engineer", "2019-07", "2021-03", cid=1032449)])
    res = check_person(FakeClient(people={URL: p}), resume(jobs=[resume().jobs[1]]), {0: COMPANIES[1]})
    c = checks(res, "Zoho", "tenure")
    assert c.verdict == Verdict.DISCREPANCY and "end off by 9 months" in c.reasons[0]


def test_claimed_current_but_profile_ended():
    p = person(past=[emp("Freshworks", "Senior Software Engineer", "2022-01", "2023-05", cid=6042422)])
    res = check_person(FakeClient(people={URL: p}), resume(jobs=[resume().jobs[0]]), {0: COMPANIES[0]})
    c = checks(res, "Freshworks", "tenure")
    assert c.verdict == Verdict.DISCREPANCY and "profile shows it ended" in c.reasons[0]


def test_missing_job_is_cannot_verify_not_contradicted():
    p = person(current=[emp("Freshworks", "Senior Software Engineer", "2022-01", cid=6042422)])
    res = check_person(FakeClient(people={URL: p}), resume(), COMPANIES)
    c = checks(res, "Zoho", "employment_on_profile")
    assert c.verdict == Verdict.CANNOT_VERIFY
    assert not any(c.verdict == Verdict.CONTRADICTED for c in res.checks)


def test_fuzzy_name_match_when_ids_missing():
    p = person(past=[emp("ZOHO Corp", "Software Engineer", "2019-07", "2021-12")])
    companies = {0: CompanyResult(job_index=0, employer="Zoho Corporation Pvt Ltd")}  # company lookup failed
    res = check_person(FakeClient(people={URL: p}), resume(jobs=[resume().jobs[1]]), companies)
    c = checks(res, "Zoho", "tenure")
    assert c.verdict == Verdict.VERIFIED and "employer name" in c.reasons[1]


def test_promotions_merge_into_one_tenure():
    p = person(current=[emp("Freshworks", "Lead Engineer", "2023-06", cid=6042422)],
               past=[emp("Freshworks", "Software Engineer", "2020-01", "2023-06", cid=6042422)])
    j = JobClaim(employer="Freshworks", title="Lead Engineer", start="2020-01", is_current=True)
    res = check_person(FakeClient(people={URL: p}), resume(jobs=[j]), {0: COMPANIES[0]})
    c = checks(res, "Freshworks", "tenure")
    assert c.verdict == Verdict.VERIFIED and "across 2 roles" in c.reasons[0]


def test_search_path_without_linkedin():
    fake = FakeClient(search_results=[AARAV])
    res = check_person(fake, resume(linkedin=None), COMPANIES)
    assert res.method == "person_search" and res.profile_url == URL
    conds = fake.searches[0]["conditions"]
    assert {"field": "basic_profile.name", "type": "(.)", "value": "Aarav Sharma"} in conds
    assert conds[1]["value"] == "Freshworks"  # most recent employer, Crustdata's name
    assert checks(res, "Freshworks", "tenure").verdict == Verdict.VERIFIED
    assert "not by a LinkedIn URL" in checks(res, "Freshworks", "tenure").reasons[1]


def test_search_ignores_other_names_and_ambiguity_is_cannot_verify():
    other = person(name="Rohit Verma", current=AARAV["experience"]["employment_details"]["current"])
    twin = person(current=[emp("Freshworks", "Designer", "2015-01", cid=6042422)],
                  past=[emp("Zoho", "Designer", "2012-01", "2014-01", cid=1032449)], url="https://x/in/twin")
    res = check_person(FakeClient(search_results=[other, AARAV, twin]), resume(linkedin=None), COMPANIES)
    assert not res.found and "cannot tell which one" in res.lookup_note
    assert all(c.verdict == Verdict.CANNOT_VERIFY for c in res.checks)


def test_enrich_failure_falls_back_to_search_then_everything_cannot_verify():
    res = check_person(FakeClient(fail={"enrich_person", "search_people"}), resume(), COMPANIES)
    assert not res.found
    for c in res.checks:
        assert c.verdict == Verdict.CANNOT_VERIFY and c.check == "employment_on_profile"
        assert any("Person Enrich failed" in r for r in c.reasons)
        assert any("Person Search failed" in r for r in c.reasons)


def test_year_only_claim():
    p = person(past=[emp("Zoho", "Software Engineer", "2019-10", "2021-12", cid=1032449)])
    j = JobClaim(employer="Zoho", title="Software Engineer", start="2019", end="2021")
    res = check_person(FakeClient(people={URL: p}), resume(jobs=[j]), {0: COMPANIES[1]})
    assert checks(res, "Zoho", "tenure").verdict == Verdict.VERIFIED


def test_blank_title_is_cannot_verify_not_discrepancy():
    p = person(past=[emp("Zoho", "Member Technical Staff", "2019-07", "2021-12", cid=1032449)])
    j = JobClaim(employer="Zoho Corporation Pvt Ltd", title="", start="2019-07", end="2021-12")
    res = check_person(FakeClient(people={URL: p}), resume(jobs=[j]), {0: COMPANIES[1]})
    t = checks(res, "Zoho", "title")
    assert t.verdict == Verdict.CANNOT_VERIFY and "no title" in t.reasons[0]
    assert t.claim == "Zoho Corporation Pvt Ltd, 2019-07 to 2021-12"
    assert checks(res, "Zoho", "tenure").verdict == Verdict.VERIFIED  # dates are still checked
