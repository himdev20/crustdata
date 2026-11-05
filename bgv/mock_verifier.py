"""A stand-in for bgv.verifier with the same signature, for use until that module exists.

Every verdict here is made up. It uses the resume's real claims so the report looks
realistic, and marks each reason, source and referee as mock so a demo can't be
mistaken for a real finding about a real person.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from bgv.models import (
    CheckResult,
    JobClaim,
    ParsedResume,
    Referee,
    Source,
    Verdict,
    VerificationReport,
)


def verify(resume: ParsedResume, run_id: str | None = None) -> VerificationReport:
    run_id = run_id or uuid.uuid4().hex[:12]
    candidate = resume.full_name or "Unnamed candidate"
    # Cycle through the jobs so a resume with one, or none, still gets every verdict.
    jobs = resume.jobs or [JobClaim(employer="Unnamed employer", title="")]
    first, second, last = jobs[0], jobs[min(1, len(jobs) - 1)], jobs[-1]

    checks = [
        _check(
            _describe(first),
            "employer_exists",
            Verdict.VERIFIED,
            f"{_employer(first)} matches a registered, active company.",
            "mock_crustdata_company_enrich",
        ),
        _check(
            _describe(second),
            "tenure",
            Verdict.DISCREPANCY,
            "The profile's dates differ from the resume's by about 4 months.",
            "mock_crustdata_person_enrich",
        ),
        _check(
            _describe(last),
            "founded_before_start",
            Verdict.CONTRADICTED,
            f"{_employer(last)} was founded after the claimed start date.",
            "mock_crustdata_company_enrich",
        ),
        _check(
            _describe_education(resume),
            "education",
            Verdict.CANNOT_VERIFY,
            "No matching record was found, so this claim needs a manual check.",
            "mock_web_search",
        ),
    ]
    for job in resume.jobs:
        if job.claimed_team_size:
            checks.append(
                _check(
                    f"Led a team of {job.claimed_team_size} at {_employer(job)}",
                    "headcount_vs_team",
                    Verdict.DISCREPANCY,
                    f"The company's headcount looks small for a team of {job.claimed_team_size}.",
                    "mock_crustdata_company_enrich",
                )
            )

    return VerificationReport(
        run_id=run_id,
        candidate=candidate,
        checks=checks,
        referees=[_referee(candidate, first)],
        decision="human_review",
    )


def _employer(job: JobClaim) -> str:
    return job.employer or "Unnamed employer"


def _describe(job: JobClaim) -> str:
    role = f"{job.title} at {_employer(job)}" if job.title else _employer(job)
    end = "present" if job.is_current else job.end or "unknown"
    return f"{role}, {job.start or 'unknown'} to {end}"


def _describe_education(resume: ParsedResume) -> str:
    if not resume.education:
        return "No education listed"
    item = resume.education[0]
    return f"{item.degree} at {item.school}" if item.degree else item.school


def _check(claim: str, check: str, verdict: Verdict, reason: str, kind: str) -> CheckResult:
    return CheckResult(
        claim=claim,
        check=check,
        verdict=verdict,
        reasons=[f"Mock result: {reason}"],
        sources=[
            Source(
                kind=kind,
                ref=f"https://example.com/mock/{check}",
                retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        ],
    )


def _referee(candidate: str, job: JobClaim) -> Referee:
    employer = _employer(job)
    return Referee(
        name="Mock Referee",
        profile_url="https://example.com/mock/referee",
        title_at_employer=f"Engineering Manager at {employer}",
        overlap=f"{job.start or 'unknown'} to {job.end or 'present'} at {employer}",
        draft_message=(
            f"Hello, {candidate} has listed time at {employer} during a background check. "
            "Could you confirm their role and dates?"
        ),
    )
