"""Candidate-profile checks: find the candidate's professional profile and compare each job claim to it.

Profile lookup
  - resume has a LinkedIn URL -> Person Enrich (falls back to Person Search if enrich finds nothing / fails)
  - otherwise                 -> Person Search by name + employer (most recent employers first)

Job matching: crustdata_company_id first (from company_checks), then fuzzy employer name.

Checks per job
  tenure   verified if start and end are both within +-3 months, else discrepancy
  title    verified if the title is similar to a title held at that employer, else discrepancy
  employment_on_profile  cannot_verify when the job is not on the profile (profiles are often incomplete,
                         so a missing job is never treated as contradicted)
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .company_checks import CompanyResult, job_label
from .crustdata_client import CrustdataClient
from .models import CheckResult, JobClaim, ParsedResume, Source, Verdict
from .utils import (company_similarity, current_month, fmt_month, month_precision, months_between,
                    normalize_company, now_iso, person_similarity, title_similarity, to_month)

DATE_TOLERANCE_MONTHS = 3
NAME_MIN = 88            # person name similarity to accept a search result
COMPANY_NAME_MIN = 85    # fuzzy employer match when ids are missing
TITLE_MIN = 75
SEARCH_FIELDS = ["basic_profile", "experience", "social_handles"]


class ProfileJob(BaseModel):
    name: str = ""
    title: str = ""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_current: bool = False
    crustdata_company_id: Optional[int] = None


class PersonResult(BaseModel):
    found: bool = False
    method: str = ""               # "person_enrich" | "person_search"
    profile_url: Optional[str] = None
    profile_name: Optional[str] = None
    lookup_note: str = ""          # how the profile was found, or why not
    profile_jobs: list[ProfileJob] = Field(default_factory=list)
    api_errors: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)


# ---------- profile lookup ----------

def profile_jobs(person_data: dict) -> list[ProfileJob]:
    ed = ((person_data or {}).get("experience") or {}).get("employment_details") or {}
    out = []
    for bucket, current in (("current", True), ("past", False)):
        for j in ed.get(bucket) or []:
            out.append(ProfileJob(
                name=j.get("name") or j.get("company_name") or "", title=j.get("title") or "",
                start_date=j.get("start_date"), end_date=j.get("end_date"),
                is_current=current and not j.get("end_date"),
                crustdata_company_id=j.get("crustdata_company_id") or j.get("company_id")))
    return out


def profile_url_of(person_data: dict) -> str | None:
    sh = (person_data or {}).get("social_handles") or {}
    return (sh.get("professional_network_identifier") or {}).get("profile_url")


def _employers_for_search(resume: ParsedResume, companies: dict[int, CompanyResult]) -> list[str]:
    """Most recent employers first, using Crustdata's name for the company when we have it."""
    order = sorted(range(len(resume.jobs)),
                   key=lambda i: (resume.jobs[i].is_current, resume.jobs[i].start or ""), reverse=True)
    names: list[str] = []
    for i in order:
        cr = companies.get(i)
        name = (cr.matched_name if cr and cr.matched_name else normalize_company(resume.jobs[i].employer))
        if name and name.lower() not in [n.lower() for n in names]:
            names.append(name)
    return names[:2]


def _claimed_employer_hits(jobs: list[ProfileJob], resume: ParsedResume, companies: dict[int, CompanyResult]) -> int:
    hits = 0
    for i, claim in enumerate(resume.jobs):
        cid = companies.get(i).crustdata_company_id if companies.get(i) else None
        if any((cid and pj.crustdata_company_id == cid) or company_similarity(claim.employer, pj.name) >= COMPANY_NAME_MIN
               for pj in jobs):
            hits += 1
    return hits


def find_profile(client: CrustdataClient, resume: ParsedResume,
                 companies: dict[int, CompanyResult]) -> tuple[PersonResult, Source | None]:
    res = PersonResult()

    if resume.linkedin_url:
        try:
            data = client.enrich_person(resume.linkedin_url)
            if data:
                res.found, res.method, res.profile_url = True, "person_enrich", resume.linkedin_url
                res.profile_name = (data.get("basic_profile") or {}).get("name")
                res.profile_jobs = profile_jobs(data)
                res.lookup_note = "Profile fetched with Person Enrich from the LinkedIn URL on the resume."
                return res, Source(kind="crustdata_person_enrich", ref=resume.linkedin_url, retrieved_at=now_iso())
            res.lookup_note = "Person Enrich found no profile for the resume's LinkedIn URL; tried Person Search. "
        except Exception as exc:
            res.api_errors.append(f"Person Enrich failed: {exc}")
            res.lookup_note = "Person Enrich failed; tried Person Search. "

    employers = _employers_for_search(resume, companies)
    if not employers:
        res.lookup_note += "No employer to search with."
        return res, None

    searched_ok = False
    for employer in employers:
        filters = {"op": "and", "conditions": [
            {"field": "basic_profile.name", "type": "(.)", "value": resume.full_name},
            {"field": "experience.employment_details.company_name", "type": "(.)", "value": employer},
        ]}
        ref = f"/person/search name={resume.full_name!r} employer={employer!r}"
        try:
            result = client.search_people(filters, limit=5, fields=SEARCH_FIELDS)
        except Exception as exc:
            res.api_errors.append(f"Person Search failed ({employer}): {exc}")
            continue
        searched_ok = True
        profiles = [p for p in (result.get("profiles") or [])
                    if person_similarity(resume.full_name, (p.get("basic_profile") or {}).get("name")) >= NAME_MIN]
        if not profiles:
            continue
        scored = sorted(((_claimed_employer_hits(profile_jobs(p), resume, companies), p) for p in profiles),
                        key=lambda t: t[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            res.lookup_note += (f"{len(scored)} profiles named '{resume.full_name}' worked at {employer}; "
                                "cannot tell which one is the candidate. Add the candidate's LinkedIn URL.")
            return res, Source(kind="crustdata_person_search", ref=ref, retrieved_at=now_iso())
        best = scored[0][1]
        res.found, res.method = True, "person_search"
        res.profile_url = profile_url_of(best)
        res.profile_name = (best.get("basic_profile") or {}).get("name")
        res.profile_jobs = profile_jobs(best)
        res.lookup_note += (f"Profile found with Person Search on name + '{employer}' (identity matched by name and "
                            f"{scored[0][0]} employer(s), not by a LinkedIn URL).")
        return res, Source(kind="crustdata_person_search", ref=f"{ref} -> {res.profile_url}", retrieved_at=now_iso())

    if searched_ok:
        res.lookup_note += f"No profile named '{resume.full_name}' found at {', '.join(employers)}."
        return res, Source(kind="crustdata_person_search",
                           ref=f"/person/search name={resume.full_name!r} employers={employers} -> 0 matches",
                           retrieved_at=now_iso())
    return res, None


# ---------- comparing dates ----------

def _claim_distance(claim: str | None, profile_month: tuple[int, int] | None, claim_is_current: bool = False) -> int | None:
    """Months between a claimed date and the profile's. A year-only claim covers Jan-Dec of that year."""
    if profile_month is None:
        return None
    if claim_is_current:
        return abs(months_between(current_month(), profile_month))
    cm = to_month(claim)
    if not cm:
        return None
    if month_precision(claim) == "year":
        if profile_month[0] == cm[0]:
            return 0
        lo, hi = (cm[0], 1), (cm[0], 12)
        return min(abs(months_between(lo, profile_month)), abs(months_between(hi, profile_month)))
    return abs(months_between(cm, profile_month))


def _span(entries: list[ProfileJob]) -> tuple[tuple[int, int] | None, tuple[int, int] | None, bool]:
    starts = [to_month(e.start_date) for e in entries if to_month(e.start_date)]
    ends = [current_month() if e.is_current else to_month(e.end_date) for e in entries]
    current = any(e.is_current for e in entries)
    return (min(starts) if starts else None,
            (max(e for e in ends if e) if any(ends) else None), current)


def _tenure_verdict(job: JobClaim, entries: list[ProfileJob]) -> tuple[Verdict, str]:
    p_start, p_end, p_current = _span(entries)
    if not to_month(job.start):
        return Verdict.CANNOT_VERIFY, "The resume gives no start date for this role."
    if p_start is None:
        return Verdict.CANNOT_VERIFY, "The profile has no start date for this role."
    d_start = _claim_distance(job.start, p_start)
    ends_known = job.is_current or job.end
    d_end = _claim_distance(job.end, p_end, job.is_current) if ends_known else 0
    p_end_txt = "present" if p_current else fmt_month(p_end)
    c_end_txt = "present" if job.is_current else (job.end or "unknown")
    detail = (f"Resume {job.start} to {c_end_txt}; profile {fmt_month(p_start)} to {p_end_txt} "
              f"(start off by {d_start} months" + (f", end off by {d_end} months)." if ends_known and d_end is not None else ")."))
    if d_end is None:
        return Verdict.CANNOT_VERIFY, detail + " The profile has no end date to compare."
    if d_start <= DATE_TOLERANCE_MONTHS and d_end <= DATE_TOLERANCE_MONTHS:
        return Verdict.VERIFIED, detail
    if job.is_current and not p_current:
        detail += " The resume says this role is current but the profile shows it ended."
    elif p_current and not job.is_current and job.end:
        detail += " The profile still shows this role as current."
    return Verdict.DISCREPANCY, detail


# ---------- matching ----------

def match_entries(job: JobClaim, company: CompanyResult | None, jobs: list[ProfileJob]) -> tuple[list[ProfileJob], str]:
    cid = company.crustdata_company_id if company else None
    if cid:
        by_id = [pj for pj in jobs if pj.crustdata_company_id == cid]
        if by_id:
            return by_id, f"matched on crustdata_company_id {cid}"
    by_name = [pj for pj in jobs
               if max(company_similarity(job.employer, pj.name),
                      company_similarity(company.matched_name, pj.name) if company and company.matched_name else 0)
               >= COMPANY_NAME_MIN]
    if by_name:
        return by_name, f"matched on employer name '{by_name[0].name}'"
    return [], ""


def check_person(client: CrustdataClient, resume: ParsedResume,
                 companies: dict[int, CompanyResult]) -> PersonResult:
    res, src = find_profile(client, resume, companies)
    sources = [src] if src else []

    for i, job in enumerate(resume.jobs):
        claim = job_label(job)
        if not res.found:
            reasons = [r for r in [res.lookup_note.strip()] if r] + res.api_errors
            res.checks.append(CheckResult(claim=claim, check="employment_on_profile", verdict=Verdict.CANNOT_VERIFY,
                                          reasons=reasons or ["No candidate profile found."], sources=sources))
            continue

        entries, how = match_entries(job, companies.get(i), res.profile_jobs)
        if not entries:
            res.checks.append(CheckResult(
                claim=claim, check="employment_on_profile", verdict=Verdict.CANNOT_VERIFY,
                reasons=[f"The candidate's profile does not list {job.employer}. Profiles are often incomplete, "
                         "so this is not treated as a contradiction; verify with the employer or a referee."],
                sources=sources))
            continue

        # Tenure: the closest single role first, then the span of all roles at this employer (promotions).
        best = min(entries, key=lambda e: (_claim_distance(job.start, to_month(e.start_date)) or 999,
                                           -title_similarity(job.title, e.title)))
        verdict, detail = _tenure_verdict(job, [best])
        if verdict != Verdict.VERIFIED and len(entries) > 1:
            v2, d2 = _tenure_verdict(job, entries)
            if v2 == Verdict.VERIFIED:
                verdict, detail = v2, d2 + f" (across {len(entries)} roles at this employer)"
        res.checks.append(CheckResult(claim=claim, check="tenure", verdict=verdict,
                                      reasons=[detail, f"Profile entry {how}. {res.lookup_note}".strip()],
                                      sources=sources))

        # Title: any role held at this employer counts.
        t_best = max(entries, key=lambda e: title_similarity(job.title, e.title))
        t_score = title_similarity(job.title, t_best.title)
        if not t_best.title:
            res.checks.append(CheckResult(claim=claim, check="title", verdict=Verdict.CANNOT_VERIFY,
                                          reasons=["The profile has no title for this role."], sources=sources))
        elif t_score >= TITLE_MIN:
            res.checks.append(CheckResult(claim=claim, check="title", verdict=Verdict.VERIFIED,
                                          reasons=[f"Profile title '{t_best.title}' matches '{job.title}' "
                                                   f"(similarity {t_score:.0f})."], sources=sources))
        else:
            res.checks.append(CheckResult(claim=claim, check="title", verdict=Verdict.DISCREPANCY,
                                          reasons=[f"Resume says '{job.title}' but the profile says "
                                                   f"'{t_best.title}' (similarity {t_score:.0f})."],
                                          sources=sources))
    return res
