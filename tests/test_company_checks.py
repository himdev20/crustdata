from bgv.company_checks import check_company, headcount_during, pick_match
from bgv.models import JobClaim, Verdict
from bgv.store import Store
from tests.fakes import FakeClient, company, web

ZOHO = company(1032449, "Zoho", "zoho.com", 1996, 30919,
               [{"date": "2021-12-27", "employee_count": 12199}, {"date": "2026-11-02", "employee_count": 30919}])
ZOHO_BE = company(9834862, "ZOHO CORPORATION BELGIUM", None, None, 2)
STARTUP = company(77, "Kiranakart Technologies Pvt Ltd", "kiranakart.in", 2021, 40)
ZOHO_WEB = [web("Zoho | Chennai", "https://www.zoho.com"), web("Zoho Corp", "https://en.wikipedia.org/wiki/Zoho"),
            web("Zoho careers", "https://careers.zohocorp.com")]


def job(**kw):
    base = dict(employer="Zoho Corporation Pvt Ltd", title="Software Engineer", start="2019-07", end="2021-12")
    base.update(kw)
    return JobClaim(**base)


def by_check(res):
    return {c.check: c for c in res.checks}


def test_pick_match_skips_wrong_entity_ranked_first():
    matches = FakeClient([ZOHO_BE, ZOHO]).identify_company(name="Zoho Corporation Pvt Ltd")
    m, score = pick_match(matches, "Zoho Corporation Pvt Ltd")
    assert m["company_data"]["crustdata_company_id"] == 1032449 and score >= 95


def test_pick_match_domain_wins_and_rejects_unrelated():
    matches = FakeClient([ZOHO_BE, ZOHO]).identify_company(domain="zoho.com")
    assert pick_match(matches, "Zoho Corp", "zoho.com")[0]["company_data"]["crustdata_company_id"] == 1032449
    assert pick_match(FakeClient([ZOHO_BE]).identify_company(name="Zoho"), "Zoho Health")[0] is None


def test_real_employer_all_verified():
    res = check_company(FakeClient([ZOHO_BE, ZOHO], {"zoho": ZOHO_WEB}), job(claimed_team_size=8), 0)
    c = by_check(res)
    assert c["employer_exists"].verdict == Verdict.VERIFIED
    assert c["founded_before_start"].verdict == Verdict.VERIFIED
    assert c["headcount_vs_team"].verdict == Verdict.VERIFIED
    assert all(ch.sources for ch in res.checks)
    assert not res.low_footprint


def test_founded_after_start_is_contradicted():
    j = job(employer="Kiranakart Technologies", start="2019-01", end="2022-01")
    res = check_company(FakeClient([STARTUP], {"kiranakart": ZOHO_WEB}), j, 0)
    c = by_check(res)["founded_before_start"]
    assert c.verdict == Verdict.CONTRADICTED and "2021" in c.reasons[0]


def test_team_bigger_than_headcount_is_discrepancy():
    j = job(employer="Kiranakart Technologies", start="2021-06", end="2023-01", claimed_team_size=120)
    c = by_check(check_company(FakeClient([STARTUP]), j, 0))["headcount_vs_team"]
    assert c.verdict == Verdict.DISCREPANCY and "120" in c.reasons[0] and "40" in c.reasons[0]


def test_headcount_uses_history_during_tenure():
    hc, basis = headcount_during(ZOHO["headcount"], "2021-06", "2022-03")
    assert hc == 12199 and "during" in basis
    hc, basis = headcount_during(ZOHO["headcount"], "2015-01", "2016-01")
    assert hc == 30919 and "latest" in basis


def test_unknown_company_without_web_footprint_is_contradicted():
    res = check_company(FakeClient([], {}), job(employer="Nimbus Quantasoft Pvt Ltd"), 0)
    assert by_check(res)["employer_exists"].verdict == Verdict.CONTRADICTED
    assert res.low_footprint


def test_unknown_company_with_web_footprint_is_cannot_verify():
    res = check_company(FakeClient([], {"nimbus": [web("Nimbus Quantasoft Pvt Ltd - Pune", "https://x.in")]}),
                        job(employer="Nimbus Quantasoft Pvt Ltd"), 0)
    assert by_check(res)["employer_exists"].verdict == Verdict.CANNOT_VERIFY


def test_api_failures_become_cannot_verify_never_contradicted():
    res = check_company(FakeClient([], fail={"identify_company", "web_search"}), job(claimed_team_size=5), 0)
    for c in res.checks:
        assert c.verdict == Verdict.CANNOT_VERIFY
    assert any("503" in r for c in res.checks for r in c.reasons)
    assert not res.low_footprint  # unknown, not low


def test_enrich_failure_keeps_identity_but_cannot_verify_rest():
    res = check_company(FakeClient([ZOHO], {"zoho": ZOHO_WEB}, fail={"enrich_company"}), job(claimed_team_size=5), 0)
    c = by_check(res)
    assert c["employer_exists"].verdict == Verdict.VERIFIED
    assert c["founded_before_start"].verdict == Verdict.CANNOT_VERIFY
    assert "Enrich failed" in c["headcount_vs_team"].reasons[0]


def test_repeat_low_footprint_employer(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    fake = FakeClient([], {})
    j = job(employer="Nimbus Quantasoft Pvt. Ltd.")
    for i, cand in enumerate(["c1", "c2", "c2"]):  # c2 twice -> still counts once
        r = check_company(fake, j, 0, store, cand, f"run{i}")
        assert "repeat_low_footprint_employer" not in by_check(r)
    r = check_company(fake, job(employer="NIMBUS QUANTASOFT PRIVATE LIMITED"), 0, store, "c3", "run9")
    c = by_check(r)["repeat_low_footprint_employer"]
    assert c.verdict == Verdict.DISCREPANCY and "2 other" in c.reasons[0]
    # rerunning the same candidate is idempotent
    r = check_company(fake, j, 0, store, "c3", "run9")
    assert by_check(r)["repeat_low_footprint_employer"].verdict == Verdict.DISCREPANCY


def test_well_known_employer_is_never_repeat_flagged(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    fake = FakeClient([ZOHO], {"zoho": ZOHO_WEB})
    for cand in ["a", "b", "c", "d"]:
        r = check_company(fake, job(), 0, store, cand, "r")
    assert "repeat_low_footprint_employer" not in by_check(r)
