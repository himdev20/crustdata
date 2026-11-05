"""Find possible referees: people at the same employer whose time there overlapped with the candidate's.

Outreach messages are DRAFTED ONLY. This module has no way to send anything; a human
reviews each draft and contacts the person only after the candidate has consented to
reference checks.
"""
from __future__ import annotations

from .company_checks import CompanyResult
from .crustdata_client import CrustdataClient
from .models import JobClaim, ParsedResume, Referee
from .person_checks import PersonResult, ProfileJob, match_entries, profile_jobs, profile_url_of
from .utils import current_month, fmt_month, month_index, normalize_company, person_similarity, to_month

MIN_OVERLAP_MONTHS = 3
SEARCH_LIMIT = 15
SENIOR_WORDS = ("manager", "lead", "head", "director", "principal", "vp", "vice president", "founder", "cto", "ceo")

Window = tuple[tuple[int, int], tuple[int, int]]


def _window_from_entries(entries: list[ProfileJob]) -> Window | None:
    starts = [to_month(e.start_date) for e in entries if to_month(e.start_date)]
    ends = [current_month() if e.is_current else to_month(e.end_date) for e in entries]
    ends = [e for e in ends if e]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def candidate_window(job: JobClaim, entries: list[ProfileJob]) -> tuple[Window | None, str]:
    """Prefer the dates on the candidate's profile; fall back to what the resume claims."""
    if entries and (w := _window_from_entries(entries)):
        return w, "profile"
    s = to_month(job.start)
    e = current_month() if job.is_current else to_month(job.end)
    if job.end and len(job.end) == 4 and e:  # year-only end -> December
        e = (e[0], 12)
    return ((s, e), "resume") if s and e else (None, "")


def overlap(a: Window, b: Window) -> tuple[Window | None, int]:
    lo = max(a[0], b[0], key=month_index)
    hi = min(a[1], b[1], key=month_index)
    months = month_index(hi) - month_index(lo)
    return ((lo, hi), months) if months > 0 else (None, 0)


def draft_message(referee_name: str, candidate: str, job: JobClaim, employer: str, ov: Window) -> str:
    first = referee_name.split()[0] if referee_name else "there"
    period = f"{fmt_month(ov[0])} to {'present' if ov[1] == current_month() else fmt_month(ov[1])}"
    role = f" as {job.title.strip()}" if job.title.strip() else ""  # resume may give no title
    return (
        f"Hi {first},\n\n"
        f"I'm part of the hiring team at [Your Company]. {candidate} has applied for a role with us, and with their "
        f"consent we are doing a short reference check. They mention working at {employer}{role}, and it "
        f"looks like your time there overlapped ({period}).\n\n"
        f"Would you be open to a 10-minute call, or a quick reply, to confirm their role and approximate dates? "
        f"Please share only what you are comfortable with; anything you say will be kept confidential.\n\n"
        f"Thank you,\n[Your Name]"
    )


def _is_candidate(profile: dict, candidate_name: str, candidate_url: str | None) -> bool:
    url = profile_url_of(profile)
    if candidate_url and url and url.rstrip("/").lower() == candidate_url.rstrip("/").lower():
        return True
    return person_similarity(candidate_name, (profile.get("basic_profile") or {}).get("name")) >= 90


def find_referees(client: CrustdataClient, resume: ParsedResume, companies: dict[int, CompanyResult],
                  person: PersonResult | None = None, max_jobs: int = 2, per_job: int = 3,
                  ) -> tuple[list[Referee], list[str]]:
    """Returns (referees, notes). Notes explain skipped jobs and API failures."""
    referees: list[Referee] = []
    notes: list[str] = []
    order = sorted(range(len(resume.jobs)),
                   key=lambda i: (resume.jobs[i].is_current, resume.jobs[i].start or ""), reverse=True)

    for i in order[:max_jobs]:
        job, company = resume.jobs[i], companies.get(i)
        own = match_entries(job, company, person.profile_jobs)[0] if person and person.found else []
        window, basis = candidate_window(job, own)
        if not window:
            notes.append(f"{job.employer}: no dates to compute overlap; skipped referee search.")
            continue
        employer_name = (company.matched_name if company and company.matched_name else normalize_company(job.employer))
        filters = {"field": "experience.employment_details.company_name", "type": "(.)", "value": employer_name}
        try:
            result = client.search_people(filters, limit=SEARCH_LIMIT,
                                          fields=["basic_profile", "experience", "social_handles"])
        except Exception as exc:
            notes.append(f"{job.employer}: referee search failed ({exc}).")
            continue

        found = []
        for p in result.get("profiles") or []:
            if _is_candidate(p, resume.full_name, person.profile_url if person else resume.linkedin_url):
                continue
            entries = match_entries(job, company, profile_jobs(p))[0]
            w = _window_from_entries(entries)
            if not w:
                continue
            ov, months = overlap(window, w)
            if not ov or months < MIN_OVERLAP_MONTHS:
                continue
            title = max(entries, key=lambda e: month_index(to_month(e.start_date) or (0, 1))).title
            senior = any(s in (title or "").lower() for s in SENIOR_WORDS)
            name = (p.get("basic_profile") or {}).get("name") or ""
            found.append((months, senior, Referee(
                name=name, profile_url=profile_url_of(p), title_at_employer=title,
                overlap=f"{fmt_month(ov[0])} to {fmt_month(ov[1])} at {employer_name} ({months} months; "
                        f"candidate dates from {basis})",
                draft_message=draft_message(name, resume.full_name, job, employer_name, ov))))
        found.sort(key=lambda t: (t[1], t[0]), reverse=True)  # seniors first, then longest overlap
        if not found:
            notes.append(f"{job.employer}: no one in the first {SEARCH_LIMIT} search results overlapped "
                         f"by {MIN_OVERLAP_MONTHS}+ months.")
        referees.extend(r for _, _, r in found[:per_job])
    return referees, notes
