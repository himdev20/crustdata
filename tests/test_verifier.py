import json

import pytest

from bgv import llm
from bgv.crustdata_client import CrustdataError
from bgv.models import CheckResult, Verdict
from bgv.store import Store
from bgv.verifier import Verifier, decide
from tests.fakes import FakeClient, company, web
from tests.test_person_checks import URL, emp, person

RESUME_TEXT = "Aarav Sharma\nlinkedin.com/in/aarav-sharma\nSoftware Engineer, Zoho, Jul 2019 - Dec 2021\n"
PARSED = {"full_name": "Aarav Sharma", "linkedin_url": URL, "jobs": [
    {"employer": "Zoho Corporation Pvt Ltd", "title": "Software Engineer", "start": "2019-07", "end": "2021-12"}]}
ZOHO = company(1032449, "Zoho", "zoho.com", 1996, 30919)
ZOHO_WEB = [web("Zoho", "https://www.zoho.com"), web("Zoho wiki", "https://en.wikipedia.org/wiki/Zoho"),
            web("Zoho careers", "https://careers.zohocorp.com")]
GOOD_PROFILE = person(past=[emp("Zoho", "Software Engineer", "2019-07", "2021-12", cid=1032449)])
COLLEAGUE = person("Priya Iyer", past=[emp("Zoho", "Engineering Manager", "2017-01", "2022-06", cid=1032449)],
                   url="https://www.linkedin.com/in/priya")


class CountingLLM:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def generate_json(self, prompt, schema):
        self.calls += 1
        return json.dumps(self.payload)


@pytest.fixture
def fake_llm():
    f = CountingLLM(PARSED)
    llm.set_provider(f)
    yield f
    llm.set_provider(None)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "runs.sqlite"))


def good_client(**kw):
    return FakeClient([ZOHO], {"zoho": ZOHO_WEB}, people={URL: GOOD_PROFILE}, search_results=[COLLEAGUE], **kw)


def test_decide():
    ok = CheckResult(claim="c", check="x", verdict=Verdict.VERIFIED, reasons=[])
    unk = CheckResult(claim="c", check="x", verdict=Verdict.CANNOT_VERIFY, reasons=[])
    assert decide([ok, ok]) == "auto_clear"
    assert decide([ok, unk]) == "human_review"
    assert decide([]) == "human_review"


def test_clean_run_auto_clears(fake_llm, store):
    rep = Verifier(good_client(), store).run(RESUME_TEXT)
    assert rep.decision == "auto_clear", [(c.check, c.verdict, c.reasons) for c in rep.checks]
    assert {c.check for c in rep.checks} == {"employer_exists", "founded_before_start", "tenure", "title"}
    assert rep.referees[0].name == "Priya Iyer"
    run = store.get_run(rep.run_id)
    assert (run["status"], run["decision"], run["review_status"]) == ("done", "auto_clear", "not_needed")


def test_discrepancy_goes_to_human_review(fake_llm, store):
    bad = person(past=[emp("Zoho", "Intern", "2020-06", "2021-01", cid=1032449)])
    rep = Verifier(FakeClient([ZOHO], {"zoho": ZOHO_WEB}, people={URL: bad}), store).run(RESUME_TEXT)
    assert rep.decision == "human_review"
    assert "Flagged: tenure" in rep.summary
    assert store.list_runs("pending")[0]["run_id"] == rep.run_id


def test_rerun_continues_from_checkpoint(fake_llm, store):
    # First run is interrupted (Ctrl-C / process killed) during the person step.
    class Interrupted(FakeClient):
        def enrich_person(self, url):
            raise KeyboardInterrupt

    c1 = Interrupted([ZOHO], {"zoho": ZOHO_WEB})
    with pytest.raises(KeyboardInterrupt):
        Verifier(c1, store).run(RESUME_TEXT)
    run_id = store.list_runs()[0]["run_id"]
    assert store.get_run(run_id)["status"] == "running"  # never finished
    assert [s["step_key"] for s in store.list_steps(run_id)] == ["parse", "company:0"]

    c2 = good_client()
    rep = Verifier(c2, store).run(RESUME_TEXT)
    assert rep.run_id == run_id
    assert fake_llm.calls == 1                       # parse not repeated
    assert "identify_company" not in c2.calls        # company step not repeated
    assert c2.calls[0] == "enrich_person"            # continued at the person step
    assert rep.decision == "auto_clear"


def test_api_failure_step_is_retried_on_rerun(fake_llm, store):
    rep1 = Verifier(good_client(fail={"enrich_person", "search_people"}), store).run(RESUME_TEXT)
    assert rep1.decision == "human_review"
    assert any("503" in r for c in rep1.checks for r in c.reasons)
    c2 = good_client()
    rep2 = Verifier(c2, store).run(RESUME_TEXT)
    assert "identify_company" not in c2.calls  # company step was clean -> reused
    assert "enrich_person" in c2.calls         # person step had an API error -> retried
    assert rep2.decision == "auto_clear"


def test_missing_api_key_becomes_cannot_verify(fake_llm, store, monkeypatch):
    monkeypatch.delenv("CRUSTDATA_API_KEY", raising=False)
    monkeypatch.setattr("bgv.crustdata_client.load_dotenv", lambda: None)
    rep = Verifier(None, store).run(RESUME_TEXT)
    assert rep.decision == "human_review"
    assert all(c.verdict == Verdict.CANNOT_VERIFY for c in rep.checks)
    assert "CRUSTDATA_API_KEY" in rep.checks[0].reasons[0]


def test_review_decision_survives_rerun(fake_llm, store):
    rep = Verifier(FakeClient(fail={"identify_company", "web_search", "enrich_person", "search_people"}),
                   store).run(RESUME_TEXT)
    store.update_run(rep.run_id, review_status="approved", reviewer_notes="checked with HR")
    Verifier(FakeClient(fail={"identify_company", "web_search", "enrich_person", "search_people"}),
             store).run(RESUME_TEXT)
    assert store.get_run(rep.run_id)["review_status"] == "approved"
