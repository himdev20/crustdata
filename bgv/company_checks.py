"""Employer checks: Company Identify -> Company Enrich -> Web Search, for each job on the resume.

Checks produced per employer
  employer_exists        verified     found in Crustdata
                         contradicted not in Crustdata AND no web footprint
                         cannot_verify not in Crustdata but on the web, or an API call failed
  founded_before_start   contradicted company founded after the claimed start year
  headcount_vs_team      discrepancy  claimed team size > company headcount (during the tenure if known)
  repeat_low_footprint_employer
                         discrepancy  a low-footprint employer already seen on 2+ other candidates' resumes

Every API failure becomes cannot_verify with the error as the reason; it never becomes contradicted.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .crustdata_client import CrustdataClient
from .models import CheckResult, JobClaim, Source, Verdict
from .store import Store
from .utils import (company_similarity, current_month, fmt_month, month_index, normalize_company, now_iso,
                    to_month)

NAME_MATCH_MIN = 88       # company_similarity needed to accept an Identify candidate
WEB_HIT_MIN_SIMILARITY = 90
LOW_FOOTPRINT_WEB_HITS = 3  # fewer relevant web results than this = low footprint
REPEAT_THRESHOLD = 2        # seen on this many *other* resumes -> discrepancy


class CompanyResult(BaseModel):
    """Output of check_company; checkpointed by the verifier and reused by person/referee checks."""
    job_index: int
    employer: str
    crustdata_company_id: Optional[int] = None
    matched_name: Optional[str] = None
    domain: Optional[str] = None
    year_founded: Optional[int] = None
    headcount: Optional[int] = None
    web_hits: Optional[int] = None
    low_footprint: bool = False
    api_errors: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)


def job_label(job: JobClaim) -> str:
    end = "present" if job.is_current else (job.end or "?")
    return f"{job.title} at {job.employer}, {job.start or '?'} to {end}"


def _src(kind: str, ref: str) -> Source:
    return Source(kind=kind, ref=ref, retrieved_at=now_iso())


# ---------- identify ----------

def pick_match(matches: list[dict], employer: str, domain: str | None = None) -> tuple[dict | None, float]:
    """Choose the Identify candidate that really is the claimed employer (not the top-ranked one blindly)."""
    best, best_score = None, 0.0
    for m in matches:
        info = (m.get("company_data") or {}).get("basic_info") or {}
        domains = {d.lower() for d in (info.get("all_domains") or [])} | {(info.get("primary_domain") or "").lower()}
        if domain and domain.lower() in domains:
            score = 100.0
        else:
            score = max(company_similarity(employer, info.get("name")),
                        company_similarity(employer, info.get("profile_name")))
        # tie-breakers: has a website, then Crustdata confidence
        key = (score, bool(info.get("primary_domain")), m.get("confidence_score") or 0)
        if best is None or key > best[0]:
            best, best_score = (key, m), score
    if best is None or best_score < NAME_MATCH_MIN:
        return None, best_score
    return best[1], best_score


# ---------- headcount ----------

def headcount_during(headcount: dict, start: str | None, end: str | None) -> tuple[int | None, str]:
    """Largest headcount seen during the tenure; falls back to the latest total."""
    total = headcount.get("total")
    s, e = to_month(start), to_month(end) or current_month()
    points = []
    for p in headcount.get("timeseries") or []:
        pm = to_month(p.get("date"))
        if pm and p.get("employee_count") is not None and s and month_index(s) <= month_index(pm) <= month_index(e):
            points.append(p["employee_count"])
    if points:
        return max(points), f"peak headcount during {fmt_month(s)} to {fmt_month(e)}"
    return total, "latest headcount (no history covers the claimed tenure)"


# ---------- web ----------

def relevant_web_hits(results: list[dict], employer: str) -> list[dict]:
    from rapidfuzz import fuzz

    name = normalize_company(employer)
    hits = []
    for r in results:
        text = normalize_company(" ".join(str(r.get(k) or "") for k in ("title", "snippet", "url")))
        if name and fuzz.partial_ratio(name, text) >= WEB_HIT_MIN_SIMILARITY:
            hits.append(r)
    return hits


def _web_ref(r: dict) -> str:
    return r.get("url") or r.get("title") or "web result"


# ---------- main ----------

def check_company(client: CrustdataClient, job: JobClaim, job_index: int, store: Store | None = None,
                  candidate_key: str = "", run_id: str = "") -> CompanyResult:
    res = CompanyResult(job_index=job_index, employer=job.employer)
    claim = job_label(job)
    checks = res.checks

    # 1. Identify (free)
    identify_ok, info, identify_src = False, {}, None
    try:
        matches: list[dict] = []
        if job.employer_domain:
            matches = client.identify_company(domain=job.employer_domain)
        if not matches:
            matches = client.identify_company(name=job.employer)
        identify_ok = True
        match, score = pick_match(matches, job.employer, job.employer_domain)
        identify_src = _src("crustdata_company_identify",
                            f"/company/identify {job.employer_domain or job.employer!r} ({len(matches)} candidates)")
        if match:
            cd = match["company_data"]
            info = cd.get("basic_info") or {}
            res.crustdata_company_id = cd.get("crustdata_company_id")
            res.matched_name = info.get("name")
            res.domain = info.get("primary_domain")
    except Exception as exc:
        res.api_errors.append(f"Company Identify failed: {exc}")

    # 2. Enrich (2 credits) only when identified
    enrich: dict[str, Any] | None = None
    if res.crustdata_company_id:
        try:
            enrich = client.enrich_company(res.crustdata_company_id)
            if enrich:
                b = enrich.get("basic_info") or {}
                yf = b.get("year_founded")
                res.year_founded = int(str(yf)[:4]) if yf and str(yf)[:4].isdigit() else None
                res.headcount = (enrich.get("headcount") or {}).get("total")
        except Exception as exc:
            res.api_errors.append(f"Company Enrich failed: {exc}")

    # 3. Web search (1 credit)
    web_ok, hits = False, []
    try:
        results = client.web_search(f"{job.employer} company")
        web_ok = True
        hits = relevant_web_hits(results, job.employer)
        res.web_hits = len(hits)
    except Exception as exc:
        res.api_errors.append(f"Web Search failed: {exc}")
    web_sources = [_src("web_search", _web_ref(h)) for h in hits[:3]]

    enrich_src = None
    if res.crustdata_company_id:
        enrich_src = _src("crustdata_company_enrich",
                          f"crustdata_company_id={res.crustdata_company_id} ({res.matched_name}"
                          f"{', ' + res.domain if res.domain else ''})")

    # ---- employer_exists ----
    if res.crustdata_company_id:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.VERIFIED,
            reasons=[f"Matched Crustdata company '{res.matched_name}' (id {res.crustdata_company_id})."
                     + (f" {res.web_hits} relevant web results." if web_ok else "")],
            sources=[s for s in [identify_src, enrich_src] if s] + web_sources))
    elif identify_ok and web_ok and not hits:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.CONTRADICTED,
            reasons=[f"'{job.employer}' was not found in Crustdata and no web search result mentions it."],
            sources=[s for s in [identify_src] if s] + [_src("web_search", f"query: {job.employer} company")]))
    elif identify_ok and web_ok:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.CANNOT_VERIFY,
            reasons=[f"Not found in Crustdata, but {len(hits)} web result(s) mention '{job.employer}'. "
                     "Confirm the entity manually (e.g. MCA registry)."],
            sources=[s for s in [identify_src] if s] + web_sources))
    else:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.CANNOT_VERIFY,
            reasons=res.api_errors or ["Company lookup did not complete."],
            sources=[s for s in [identify_src] if s] + web_sources))

    # ---- founded_before_start ----
    start = to_month(job.start)
    if res.crustdata_company_id:
        if not start:
            checks.append(CheckResult(claim=claim, check="founded_before_start", verdict=Verdict.CANNOT_VERIFY,
                                      reasons=["The resume gives no start date for this role."]))
        elif res.year_founded is None:
            reason = next((e for e in res.api_errors if "Enrich" in e), "Crustdata has no founding year.")
            checks.append(CheckResult(claim=claim, check="founded_before_start", verdict=Verdict.CANNOT_VERIFY,
                                      reasons=[reason], sources=[s for s in [enrich_src] if s]))
        elif res.year_founded > start[0]:
            checks.append(CheckResult(
                claim=claim, check="founded_before_start", verdict=Verdict.CONTRADICTED,
                reasons=[f"{res.matched_name} was founded in {res.year_founded}, after the claimed start "
                         f"{job.start}."], sources=[enrich_src]))
        else:
            note = " (same year; month not checkable)" if res.year_founded == start[0] else ""
            checks.append(CheckResult(
                claim=claim, check="founded_before_start", verdict=Verdict.VERIFIED,
                reasons=[f"Founded {res.year_founded}, before the claimed start {job.start}{note}."],
                sources=[enrich_src]))

    # ---- headcount_vs_team ----
    if job.claimed_team_size:
        if enrich and enrich.get("headcount"):
            hc, basis = headcount_during(enrich["headcount"], job.start, job.end)
            if hc is None:
                checks.append(CheckResult(claim=claim, check="headcount_vs_team", verdict=Verdict.CANNOT_VERIFY,
                                          reasons=["Crustdata has no headcount for this company."],
                                          sources=[enrich_src]))
            elif job.claimed_team_size > hc:
                checks.append(CheckResult(
                    claim=claim, check="headcount_vs_team", verdict=Verdict.DISCREPANCY,
                    reasons=[f"Claims a team of {job.claimed_team_size}, but the company's {basis} was {hc}."],
                    sources=[enrich_src]))
            else:
                checks.append(CheckResult(
                    claim=claim, check="headcount_vs_team", verdict=Verdict.VERIFIED,
                    reasons=[f"Team of {job.claimed_team_size} is plausible: {basis} was {hc}."],
                    sources=[enrich_src]))
        else:
            reason = next((e for e in res.api_errors if "Enrich" in e or "Identify" in e),
                          "Company not found in Crustdata, so headcount is unknown.")
            checks.append(CheckResult(claim=claim, check="headcount_vs_team", verdict=Verdict.CANNOT_VERIFY,
                                      reasons=[reason]))

    # ---- low footprint + repeat employer ----
    lookup_done = identify_ok and (web_ok or res.crustdata_company_id is None)
    res.low_footprint = bool(lookup_done and (res.crustdata_company_id is None
                                              or (web_ok and len(hits) < LOW_FOOTPRINT_WEB_HITS)))
    if store is not None and candidate_key:
        key = normalize_company(job.employer)
        if res.low_footprint:
            others = store.other_candidates_with_employer(key, candidate_key)
            if len(others) >= REPEAT_THRESHOLD:
                checks.append(CheckResult(
                    claim=claim, check="repeat_low_footprint_employer", verdict=Verdict.DISCREPANCY,
                    reasons=[f"'{job.employer}' has a low footprint (Crustdata: "
                             f"{'found' if res.crustdata_company_id else 'not found'}, "
                             f"{res.web_hits if res.web_hits is not None else '?'} web hits) and already appears on "
                             f"{len(others)} other candidates' resumes. Possible fake-employer pattern."],
                    sources=[_src("bgv_history", f"run {o['run_id']} ({o['seen_at']})") for o in others[:5]]))
        store.record_employer(key, candidate_key, job.employer, run_id, res.low_footprint)

    return res
