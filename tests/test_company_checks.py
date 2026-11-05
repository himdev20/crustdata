from bgv.company_checks import check_company, headcount_during, independent_web_hits, pick_match
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


def test_one_mention_is_not_a_footprint_contradicted():
    """A single page that mentions the name (here a directory listing) is not real presence."""
    results = [web("Details of NEXORA INFOTECH SOLUTIONS LLP", "https://www.zaubacorp.com/company/NEXORA",
                   "Company Name, NEXORA INFOTECH SOLUTIONS LLP")]
    c = by_check(check_company(FakeClient([], {"nexora": results}), job(employer="Nexora Infotech Solutions Pvt Ltd"), 0))
    assert c["employer_exists"].verdict == Verdict.CONTRADICTED
    assert "No record in Crustdata and no independent web presence" in c["employer_exists"].reasons[0]


def test_two_independent_sources_without_crustdata_is_discrepancy():
    results = [web("Nexora wins award", "https://economictimes.indiatimes.com/x",
                   "Pune-based Nexora Infotech Solutions won ..."),
               web("Startups to watch", "https://yourstory.com/y", "Nexora Infotech Solutions Pvt Ltd, founded ...")]
    c = by_check(check_company(FakeClient([], {"nexora": results}), job(employer="Nexora Infotech Solutions Pvt Ltd"), 0))
    assert c["employer_exists"].verdict == Verdict.DISCREPANCY
    assert len(c["employer_exists"].sources) == 3  # identify + 2 independent pages


def test_independent_web_hits_filters_sources():
    name = "Nexora Infotech Solutions Pvt Ltd"
    results = [
        web("Home", "https://nexora-infotech.in/about", "Nexora Infotech Solutions builds ..."),     # own site
        web("Nexora", "https://www.google.com/goto?url=CAESbQ", "Nexora Infotech Solutions ..."),   # hidden source
        web("Jobs", "https://www.naukri.com/nexora-jobs", "Nexora Infotech Solutions is hiring"),   # job board
        web("Listing", "https://in.linkedin.com/company/nexora", "Nexora Infotech Solutions ..."),  # social
        web("Directory", "https://www.justdial.com/Pune/nexora", "Nexora Infotech Solutions, Pune"),  # listing
        web("Other co", "https://news.example.com/a", "Nexora Technologies raised funding"),       # name differs
        web("Partial", "https://news.example.com/b", "Nexora Infotech is ..."),                    # not exact name
        web("Real", "https://news.example.com/c", "NEXORA INFOTECH SOLUTIONS PRIVATE LIMITED signed ..."),
    ]
    hits = independent_web_hits(results, name, {"nexora.co"})
    assert [h["title"] for h in hits] == ["Real"]


def test_company_own_domain_from_crustdata_is_not_independent():
    results = [web("About", "https://careers.zoho.com/x", "Zoho Corporation is ..."),
               web("Wiki", "https://en.wikipedia.org/wiki/Zoho", "Zoho Corporation is an Indian ...")]
    assert [h["title"] for h in independent_web_hits(results, "Zoho Corporation", {"zoho.com"})] == ["Wiki"]


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


# ---------- ambiguous names ----------

HCL_UK = company(1166098, "HCL", "hcl.co.uk", 2013, 1872, country="GBR")
HCL_SMALL = company(8911409, "HCL", None, 2015, 6, country="USA")
HCL_INFO = company(1909, "HCL Infosystems Ltd.", "hclinfosystems.in", 1976, 12000, country="IND")
HCL_HEALTH = company(1268660, "HCL Healthcare", "hclhealthcare.in", 2011, 3000, country="IND")
HCL_WEB = [web("HCLTech", "https://en.wikipedia.org/wiki/HCLTech", "HCL Technologies Limited is ...")]


def hcl_job(**kw):
    return job(employer="HCL", title="Trainee", start="2016-01", end="2017-06", **kw)


def test_short_name_prefers_indian_hq_then_largest_headcount():
    fake = FakeClient([HCL_UK, HCL_SMALL, HCL_INFO, HCL_HEALTH], {"hcl": HCL_WEB})
    res = check_company(fake, hcl_job(), 0)
    assert res.crustdata_company_id == 1909          # Indian, and 4x HCL Healthcare's headcount
    assert "headquartered in India" in res.match_note and "largest headcount" in res.match_note
    assert by_check(res)["employer_exists"].verdict == Verdict.VERIFIED


def test_short_name_still_ambiguous_is_cannot_verify_with_candidates_and_no_founding_check():
    hcl_us = company(19743868, "hcl", None, 2020, 1500, country="USA")
    fake = FakeClient([HCL_UK, hcl_us], {"hcl": HCL_WEB})          # no Indian HQ, similar headcounts
    res = check_company(fake, hcl_job(claimed_team_size=5), 0)
    c = by_check(res)
    assert c["employer_exists"].verdict == Verdict.CANNOT_VERIFY
    reason = c["employer_exists"].reasons[0]
    assert "ambiguous" in reason and "id 1166098" in reason and "id 19743868" in reason and "HQ GBR" in reason
    assert "founded_before_start" not in c                          # 2020 > 2016 would wrongly contradict
    assert c["headcount_vs_team"].verdict == Verdict.CANNOT_VERIFY and "ambiguous" in c["headcount_vs_team"].reasons[0]
    assert res.crustdata_company_id is None and not res.low_footprint


def test_resume_domain_beats_every_other_tie_break():
    fake = FakeClient([HCL_UK, HCL_INFO, HCL_HEALTH], {"hcl": HCL_WEB})
    res = check_company(fake, hcl_job(employer_domain="hclhealthcare.in"), 0)
    assert res.crustdata_company_id == 1268660 and "domain" in res.match_note
    assert fake.enriched == [1268660]                               # no tie-break enrichment needed


def test_similar_long_names_are_resolved_like_short_ones():
    a = company(1, "Acme Software Solutions", "acme.in", 2005, 800, country="IND")
    b = company(2, "Acme Softwares Solution", "acme.com", 2001, 900, country="USA")
    res = check_company(FakeClient([b, a]), job(employer="Acme Software Solutions Pvt Ltd"), 0)
    assert res.crustdata_company_id == 1 and "2 similar candidates" in res.match_note


def test_zoho_vs_zoho_belgium_by_indian_hq():
    zoho = company(1032449, "Zoho", "zoho.com", 1996, 30919, country="IND")
    belgium = company(9834862, "ZOHO CORPORATION BELGIUM", None, None, 2, country="BEL")
    res = check_company(FakeClient([belgium, zoho], {"zoho": ZOHO_WEB}), job(), 0)
    assert res.crustdata_company_id == 1032449


def test_enrich_failure_while_breaking_a_tie_is_cannot_verify_not_contradicted():
    res = check_company(FakeClient([HCL_UK, HCL_INFO], {"hcl": []}, fail={"enrich_company"}), hcl_job(), 0)
    c = by_check(res)["employer_exists"]
    assert c.verdict == Verdict.CANNOT_VERIFY and "resolving similar candidates" in c.reasons[0]
    assert not res.low_footprint


def test_tiny_crustdata_company_without_web_presence_is_low_footprint():
    shell = company(5, "Quantasoft Labs", None, 2019, 3, country="IND")
    assert check_company(FakeClient([shell]), job(employer="Quantasoft Labs"), 0).low_footprint
    big = company(6, "Quantasoft Labs", None, 2019, 900, country="IND")
    assert not check_company(FakeClient([big]), job(employer="Quantasoft Labs"), 0).low_footprint


def test_blank_title_claim_label():
    from bgv.company_checks import job_label
    assert job_label(job(employer="Tech Mahindra", title="", start="2017-08", end="2019-02")) == \
        "Tech Mahindra, 2017-08 to 2019-02"
