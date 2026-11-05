"""date_consistency: checks a resume's dates against each other and against today. No API calls.

  contradicted  the end is before the start, or the start is in the future
  discrepancy   two full-time jobs overlap by more than 3 months
  cannot_verify the resume gives no start date, or no end date for a past role
  verified      otherwise

A year-only date ("2021") could be any month of that year. For "contradicted" the most lenient
month is used (start = Jan, end = Dec), and for overlaps the narrowest, so a vague date never
produces a false flag on its own.
"""
from __future__ import annotations

import re

from .company_checks import job_label
from .models import CheckResult, JobClaim, ParsedResume, Source, Verdict
from .utils import current_month, fmt_month, month_index, month_precision, now_iso, to_month

MAX_OVERLAP_MONTHS = 3
# Roles that can legitimately run alongside a full-time job.
NOT_FULL_TIME = re.compile(
    r"\b(intern|internship|part[\s-]?time|freelance[r]?|freelancing|contract(ual|or)?|volunteer(ing)?|"
    r"visiting|adjunct|advis(or|er|ory)|board member|mentor|moonlighting)\b", re.I)


def _bounds(value: str | None) -> tuple[int, int] | None:
    """(earliest, latest) month index for a 'YYYY' or 'YYYY-MM' value."""
    m = to_month(value)
    if not m:
        return None
    if month_precision(value) == "year":
        return month_index((m[0], 1)), month_index((m[0], 12))
    return month_index(m), month_index(m)


def _end_bounds(job: JobClaim) -> tuple[int, int] | None:
    if job.is_current:
        now = month_index(current_month())
        return now, now
    return _bounds(job.end)


def is_full_time(job: JobClaim) -> bool:
    return not NOT_FULL_TIME.search(" ".join([job.title, job.employer, job.raw_text]))


def _fmt(idx: int) -> str:
    return fmt_month((idx // 12, idx % 12 + 1))


def check_dates(resume: ParsedResume) -> list[CheckResult]:
    now = month_index(current_month())
    spans: dict[int, tuple[int, int, int, int]] = {}  # job -> (start_lo, start_hi, end_lo, end_hi)
    problems: dict[int, list[str]] = {i: [] for i in range(len(resume.jobs))}

    for i, job in enumerate(resume.jobs):
        s, e = _bounds(job.start), _end_bounds(job)
        if s and s[0] > now:
            problems[i].append(f"The start date {job.start} is in the future.")
        if s and e and e[1] < s[0]:
            problems[i].append(f"The end date {job.end} is before the start date {job.start}.")
        if s and e and not problems[i]:
            spans[i] = (*s, *e)

    contradicted = {i for i, p in problems.items() if p}
    overlaps: dict[int, list[str]] = {i: [] for i in range(len(resume.jobs))}
    full_time = [i for i in spans if is_full_time(resume.jobs[i])]
    for a_pos, a in enumerate(full_time):
        for b in full_time[a_pos + 1:]:
            _, a_start_hi, a_end_lo, _ = spans[a]
            _, b_start_hi, b_end_lo, _ = spans[b]
            months = min(a_end_lo, b_end_lo) - max(a_start_hi, b_start_hi)  # guaranteed overlap
            if months > MAX_OVERLAP_MONTHS:
                lo, hi = max(a_start_hi, b_start_hi), min(a_end_lo, b_end_lo)
                for x, y in ((a, b), (b, a)):
                    overlaps[x].append(f"Overlaps by {months} months ({_fmt(lo)} to {_fmt(hi)}) with the full-time "
                                       f"role '{job_label(resume.jobs[y])}'.")

    results = []
    for i, job in enumerate(resume.jobs):
        claim = job_label(job)
        if i in contradicted:
            verdict, reasons = Verdict.CONTRADICTED, problems[i]
        elif overlaps[i]:
            verdict, reasons = Verdict.DISCREPANCY, overlaps[i]
        elif not to_month(job.start):
            verdict, reasons = Verdict.CANNOT_VERIFY, ["The resume gives no start date for this role."]
        elif not job.is_current and not to_month(job.end):
            verdict, reasons = Verdict.CANNOT_VERIFY, ["The resume gives no end date for this role, so overlaps "
                                                       "with other jobs cannot be checked."]
        else:
            kind = "full-time" if is_full_time(job) else "not full-time, so overlaps are allowed"
            verdict, reasons = Verdict.VERIFIED, [f"Dates are in order, not in the future, and do not overlap "
                                                  f"another full-time role by more than {MAX_OVERLAP_MONTHS} "
                                                  f"months ({kind})."]
        cited = [resume.jobs[i]] + [resume.jobs[j] for j in range(len(resume.jobs))
                                    if j != i and any(job_label(resume.jobs[j]) in r for r in overlaps[i])]
        sources = [Source(kind="resume", ref=" ".join((j.raw_text or job_label(j)).split()), retrieved_at=now_iso()) for j in cited]
        results.append(CheckResult(claim=claim, check="date_consistency", verdict=verdict, reasons=reasons,
                                   sources=sources))
    return results
