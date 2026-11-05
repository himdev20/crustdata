"""Employer checks: Company Identify -> Company Enrich -> Web Search, for each job on the resume.

Checks produced per employer
  employer_exists        verified      matched one Crustdata company
                         contradicted  not in Crustdata and fewer than 2 independent web results
                         discrepancy   not in Crustdata but 2+ independent web results
                         cannot_verify the name matches several companies and none can be singled out,
                                       or an API call failed
  founded_before_start   contradicted  company founded after the claimed start year (never run on an
                                       ambiguous match)
  headcount_vs_team      discrepancy   claimed team size > company headcount (during the tenure if known)
  repeat_low_footprint_employer
                         discrepancy   a low-footprint employer already seen on 2+ other candidates' resumes

"Independent" web results come from a site that is not the company's own, not a job board, directory,
registry, listing or social site, and whose snippet contains the exact normalised company name. Results
whose publishing site is hidden (search-engine redirect links) are not counted.

Every API failure becomes cannot_verify with the error as the reason; it never becomes contradicted.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .crustdata_client import CrustdataClient
from .models import CheckResult, JobClaim, Source, Verdict
from .store import Store
from .utils import (company_core, company_similarity, current_month, fmt_month, month_index, normalize_company,
                    now_iso, to_month)

NAME_MATCH_MIN = 88         # company_similarity needed to accept an Identify candidate
CLOSE_SCORE_GAP = 5         # candidates within this many points of the best are "similar"
SHORT_NAME_MAX = 4          # normalised names this short ("HCL", "TCS") are treated as ambiguous
MAX_AMBIGUOUS_ENRICH = 5    # candidates enriched to break a tie (2 credits each)
HEADCOUNT_DOMINANCE = 3     # largest headcount must be this many times the next to win a tie
WEB_HIT_MIN_SIMILARITY = 90
MIN_INDEPENDENT_HITS = 2    # fewer than this, and not in Crustdata -> contradicted
TINY_HEADCOUNT = 10         # a Crustdata company this small, with little web presence, is low footprint
REPEAT_THRESHOLD = 2        # seen on this many *other* resumes -> discrepancy
TIE_BREAK_FIELDS = ["basic_info", "headcount", "locations"]

# Sites whose pages say nothing independent about an employer: job boards, company directories and
# registries, business listings, data brokers, social networks and search-engine redirects.
NON_INDEPENDENT_HOSTS = (
    "google.com", "bing.com", "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "naukri.com", "indeed.com", "glassdoor.com", "glassdoor.co.in", "ambitionbox.com",
    "foundit.in", "monsterindia.com", "shine.com", "timesjobs.com", "internshala.com", "apna.co",
    "instahyre.com", "cutshort.io", "wellfound.com", "angel.co", "hirist.tech", "iimjobs.com",
    "justdial.com", "indiamart.com", "tradeindia.com", "sulekha.com", "yellowpages.com", "clutch.co",
    "goodfirms.co", "zaubacorp.com", "tofler.in", "thecompanycheck.com", "falconebiz.com",
    "instafinancials.com", "opencorporates.com", "mycorporateinfo.com", "quickcompany.in",
    "companydetails.in", "corpository.com", "crunchbase.com", "zoominfo.com", "dnb.com", "apollo.io",
    "rocketreach.co", "signalhire.com", "owler.com", "tracxn.com", "bizapedia.com", "craft.co",
)


class CompanyResult(BaseModel):
    """Output of check_company; checkpointed by the verifier and reused by person/referee checks."""
    job_index: int
    employer: str
    crustdata_company_id: Optional[int] = None
    matched_name: Optional[str] = None
    domain: Optional[str] = None
    year_founded: Optional[int] = None
    headcount: Optional[int] = None
    hq_country: Optional[str] = None
    web_hits: Optional[int] = None              # results that mention the name at all
    independent_web_hits: Optional[int] = None  # results from independent sources
    ambiguous_candidates: list[str] = Field(default_factory=list)
    match_note: str = ""
    low_footprint: bool = False
    api_errors: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)


def job_label(job: JobClaim) -> str:
    end = "present" if job.is_current else (job.end or "?")
    role = f"{job.title} at {job.employer}" if job.title.strip() else job.employer
    return f"{role}, {job.start or '?'} to {end}"


def _src(kind: str, ref: str) -> Source:
    return Source(kind=kind, ref=ref, retrieved_at=now_iso())


# ---------- identify ----------

def _info(match: dict) -> dict:
    return (match.get("company_data") or {}).get("basic_info") or {}


def _domains(info: dict) -> set[str]:
    return {d.lower() for d in (info.get("all_domains") or []) if d} | (
        {info["primary_domain"].lower()} if info.get("primary_domain") else set())


def _score(match: dict, employer: str, domain: str | None) -> float:
    info = _info(match)
    if domain and domain.lower() in _domains(info):
        return 100.0
    return max(company_similarity(employer, info.get("name")), company_similarity(employer, info.get("profile_name")))


def pick_match(matches: list[dict], employer: str, domain: str | None = None) -> tuple[dict | None, float]:
    """The best-scoring Identify candidate (not Crustdata's top-ranked one blindly), or None below the bar."""
    best, best_key, best_score = None, None, 0.0
    for m in matches:
        score = _score(m, employer, domain)
        key = (score, bool(_info(m).get("primary_domain")), m.get("confidence_score") or 0)
        if best_key is None or key > best_key:
            best, best_key, best_score = m, key, score
    if best is None or best_score < NAME_MATCH_MIN:
        return None, best_score
    return best, best_score


def _describe(cid: int, info: dict, enrich: dict | None) -> str:
    country = ((enrich or {}).get("locations") or {}).get("country")
    total = ((enrich or {}).get("headcount") or {}).get("total")
    parts = [f"id {cid}", info.get("primary_domain") or "no website",
             f"HQ {country}" if country else "HQ unknown", f"headcount {total}" if total is not None else "headcount unknown"]
    return f"{info.get('name')} ({', '.join(parts)})"


def resolve_company(client: CrustdataClient, matches: list[dict], employer: str, domain: str | None,
                    ) -> tuple[dict | None, dict | None, list[str], str]:
    """Returns (match, enrich_data_if_fetched, ambiguous_candidates, note).

    match is None with a non-empty candidate list when the name is ambiguous and no tie-break settles it.
    """
    if domain:
        by_domain = [m for m in matches if domain.lower() in _domains(_info(m))]
        if len(by_domain) == 1:
            return by_domain[0], None, [], f"matched on the resume's domain {domain}"
        if by_domain:
            matches = by_domain

    best, best_score = pick_match(matches, employer, domain)
    if best is None:
        return None, None, [], ""
    norm = normalize_company(employer)
    short = len(norm.replace(" ", "")) <= SHORT_NAME_MAX
    close = [m for m in matches
             if _score(m, employer, domain) >= max(NAME_MATCH_MIN, best_score - CLOSE_SCORE_GAP)
             # a short name could be any company whose name starts with it ("HCL" -> "HCL Infosystems")
             or (short and normalize_company(_info(m).get("name")).split()[:1] == norm.split()[:1])]
    if len(close) == 1:
        return best, None, [], ""

    # Ambiguous: enrich the closest candidates and prefer an Indian HQ, then a clearly larger headcount.
    close.sort(key=lambda m: (_score(m, employer, domain), m.get("confidence_score") or 0), reverse=True)
    pool = []
    for m in close[:MAX_AMBIGUOUS_ENRICH]:
        cid = (m.get("company_data") or {}).get("crustdata_company_id")
        enrich = client.enrich_company(cid, fields=TIE_BREAK_FIELDS)  # errors propagate to the caller
        pool.append((m, enrich or {}))
    why = (f"short name, {len(close)} companies start with it" if short else f"{len(close)} similar candidates")
    described = [_describe(m["company_data"]["crustdata_company_id"], _info(m), e) for m, e in pool]

    indian = [(m, e) for m, e in pool if (e.get("locations") or {}).get("country") == "IND"]
    if indian:
        pool, basis = indian, "headquartered in India"
    else:
        basis = "no candidate headquartered in India"
    if len(pool) == 1:
        m, e = pool[0]
        return m, e, [], f"ambiguous name ({why}); chose {_describe(m['company_data']['crustdata_company_id'], _info(m), e)}: {basis}"

    sized = sorted(pool, key=lambda t: (t[1].get("headcount") or {}).get("total") or 0, reverse=True)
    top, nxt = ((sized[0][1].get("headcount") or {}).get("total") or 0,
                (sized[1][1].get("headcount") or {}).get("total") or 0)
    if top and top >= HEADCOUNT_DOMINANCE * max(nxt, 1):
        m, e = sized[0]
        return m, e, [], (f"ambiguous name ({why}); chose {_describe(m['company_data']['crustdata_company_id'], _info(m), e)}: "
                          f"{basis}, largest headcount")
    return None, None, described, f"ambiguous name ({why})"


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
    """Results that mention the name anywhere (title, snippet or URL), fuzzily."""
    from rapidfuzz import fuzz

    name = normalize_company(employer)
    hits = []
    for r in results:
        text = normalize_company(" ".join(str(r.get(k) or "") for k in ("title", "snippet", "url")))
        if name and fuzz.partial_ratio(name, text) >= WEB_HIT_MIN_SIMILARITY:
            hits.append(r)
    return hits


def _host(url: str | None) -> str:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_listed(host: str, hosts: tuple[str, ...] | set[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _looks_like_own_site(host: str, employer: str, own_domains: set[str]) -> bool:
    if _is_listed(host, own_domains):
        return True
    label = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0])  # "nexora-infotech.co" -> "nexorainfotechco"
    for name in (normalize_company(employer), company_core(employer)):
        compact = name.replace(" ", "")
        if len(compact) >= 4 and compact in label:
            return True
    return False


def mentions_exact_name(text: str | None, employer: str) -> bool:
    name = normalize_company(employer)
    return bool(name) and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", normalize_company(text)) is not None


def independent_web_hits(results: list[dict], employer: str, own_domains: set[str] | None = None) -> list[dict]:
    own = {d.lower() for d in (own_domains or set()) if d}
    hits = []
    for r in results:
        host = _host(r.get("url"))
        if not host or _is_listed(host, NON_INDEPENDENT_HOSTS) or _looks_like_own_site(host, employer, own):
            continue
        if mentions_exact_name(r.get("snippet"), employer):
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

    # 1. Identify (free), then resolve which candidate is the employer
    identify_ok, identify_src, enrich, match = False, None, None, None
    try:
        matches: list[dict] = []
        if job.employer_domain:
            matches = client.identify_company(domain=job.employer_domain)
        if not matches:
            matches = client.identify_company(name=job.employer)
        identify_src = _src("crustdata_company_identify",
                            f"/company/identify {job.employer_domain or job.employer!r} ({len(matches)} candidates)")
        try:
            match, enrich, res.ambiguous_candidates, res.match_note = resolve_company(
                client, matches, job.employer, job.employer_domain)
            identify_ok = True  # the lookup is only complete once the candidates are resolved
        except Exception as exc:
            res.api_errors.append(f"Company Enrich failed while resolving similar candidates: {exc}")
        if match:
            cd = match["company_data"]
            info = cd.get("basic_info") or {}
            res.crustdata_company_id = cd.get("crustdata_company_id")
            res.matched_name = info.get("name")
            res.domain = info.get("primary_domain")
    except Exception as exc:
        res.api_errors.append(f"Company Identify failed: {exc}")
    ambiguous = bool(res.ambiguous_candidates)

    # 2. Enrich (2 credits) when identified and not already fetched while breaking a tie
    if res.crustdata_company_id and enrich is None:
        try:
            enrich = client.enrich_company(res.crustdata_company_id)
        except Exception as exc:
            res.api_errors.append(f"Company Enrich failed: {exc}")
    if enrich:
        yf = (enrich.get("basic_info") or {}).get("year_founded")
        res.year_founded = int(str(yf)[:4]) if yf and str(yf)[:4].isdigit() else None
        res.headcount = (enrich.get("headcount") or {}).get("total")
        res.hq_country = (enrich.get("locations") or {}).get("country")

    # 3. Web search (1 credit)
    web_ok, independent = False, []
    try:
        results = client.web_search(f"{job.employer} company")
        web_ok = True
        res.web_hits = len(relevant_web_hits(results, job.employer))
        own = {d for d in (res.domain, job.employer_domain) if d}
        independent = independent_web_hits(results, job.employer, own)
        res.independent_web_hits = len(independent)
    except Exception as exc:
        res.api_errors.append(f"Web Search failed: {exc}")
    web_sources = [_src("web_search", _web_ref(h)) for h in independent[:3]]
    web_query_src = _src("web_search", f"query: {job.employer} company")
    web_detail = (f"{res.web_hits} web result(s) mention the name, {res.independent_web_hits} from independent "
                  "sources" if web_ok else "")

    enrich_src = None
    if res.crustdata_company_id:
        enrich_src = _src("crustdata_company_enrich",
                          f"crustdata_company_id={res.crustdata_company_id} ({res.matched_name}"
                          f"{', ' + res.domain if res.domain else ''})")
    note = f" Match: {res.match_note}." if res.match_note else ""

    # ---- employer_exists ----
    if res.crustdata_company_id:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.VERIFIED,
            reasons=[f"Matched Crustdata company '{res.matched_name}' (id {res.crustdata_company_id})."
                     + note + (f" {web_detail}." if web_ok else "")],
            sources=[s for s in [identify_src, enrich_src] if s] + web_sources))
    elif ambiguous:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.CANNOT_VERIFY,
            reasons=[f"'{job.employer}' is ambiguous: it matches {len(res.ambiguous_candidates)} Crustdata companies "
                     "and none can be singled out by domain, Indian HQ or headcount. Candidates: "
                     + "; ".join(res.ambiguous_candidates) + ". Ask for the employer's website or registered name."],
            sources=[s for s in [identify_src] if s]))
    elif identify_ok and web_ok and len(independent) < MIN_INDEPENDENT_HITS:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.CONTRADICTED,
            reasons=[f"No record in Crustdata and no independent web presence ({web_detail}; "
                     f"{MIN_INDEPENDENT_HITS} needed)."],
            sources=[s for s in [identify_src] if s] + (web_sources or [web_query_src])))
    elif identify_ok and web_ok:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.DISCREPANCY,
            reasons=[f"No record in Crustdata, but {len(independent)} independent web source(s) mention "
                     f"'{job.employer}'. Confirm the entity manually (e.g. MCA registry)."],
            sources=[s for s in [identify_src] if s] + web_sources))
    else:
        checks.append(CheckResult(
            claim=claim, check="employer_exists", verdict=Verdict.CANNOT_VERIFY,
            reasons=res.api_errors or ["Company lookup did not complete."],
            sources=[s for s in [identify_src] if s] + web_sources))

    # ---- founded_before_start (never against an ambiguous match: there is none) ----
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
                         f"{job.start}." + note], sources=[enrich_src]))
        else:
            same = " (same year; month not checkable)" if res.year_founded == start[0] else ""
            checks.append(CheckResult(
                claim=claim, check="founded_before_start", verdict=Verdict.VERIFIED,
                reasons=[f"Founded {res.year_founded}, before the claimed start {job.start}{same}."],
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
            if ambiguous:
                reason = "The employer name is ambiguous, so its headcount is unknown."
            else:
                reason = next((e for e in res.api_errors if "Enrich" in e or "Identify" in e),
                              "Company not found in Crustdata, so headcount is unknown.")
            checks.append(CheckResult(claim=claim, check="headcount_vs_team", verdict=Verdict.CANNOT_VERIFY,
                                      reasons=[reason]))

    # ---- low footprint + repeat employer ----
    if not identify_ok or ambiguous:
        res.low_footprint = False  # unknown, not low
    elif res.crustdata_company_id is None:
        res.low_footprint = True
    else:
        res.low_footprint = bool(web_ok and res.headcount is not None and res.headcount <= TINY_HEADCOUNT
                                 and len(independent) < MIN_INDEPENDENT_HITS)
    if store is not None and candidate_key:
        key = normalize_company(job.employer)
        if res.low_footprint:
            others = store.other_candidates_with_employer(key, candidate_key)
            if len(others) >= REPEAT_THRESHOLD:
                checks.append(CheckResult(
                    claim=claim, check="repeat_low_footprint_employer", verdict=Verdict.DISCREPANCY,
                    reasons=[f"'{job.employer}' has a low footprint (Crustdata: "
                             f"{'found' if res.crustdata_company_id else 'not found'}, "
                             f"{res.independent_web_hits if res.independent_web_hits is not None else '?'} "
                             f"independent web sources) and already appears on {len(others)} other candidates' "
                             "resumes. Possible fake-employer pattern."],
                    sources=[_src("bgv_history", f"run {o['run_id']} ({o['seen_at']})") for o in others[:5]]))
        store.record_employer(key, candidate_key, job.employer, run_id, res.low_footprint)

    return res
