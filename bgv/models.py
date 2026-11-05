"""Shared data contract between the parser (Person B) and the verifier (Person A).

Agree on this file first, merge it to main, and then only change it together.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- What the resume claims (output of resume_parser.py) ----------

class JobClaim(BaseModel):
    employer: str
    employer_domain: Optional[str] = None  # if the resume shows a website or email domain
    title: str
    start: Optional[str] = None  # "YYYY-MM"; None if the resume does not say
    end: Optional[str] = None    # "YYYY-MM", or None if current or unknown
    is_current: bool = False
    claimed_team_size: Optional[int] = None  # e.g. "led a team of 15" -> 15
    raw_text: str = ""  # the resume line(s) this claim came from, for citations


class EducationClaim(BaseModel):
    school: str
    degree: Optional[str] = None
    field: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None


class ParsedResume(BaseModel):
    full_name: str
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    location: Optional[str] = None
    jobs: list[JobClaim] = Field(default_factory=list)
    education: list[EducationClaim] = Field(default_factory=list)


# ---------- What the agent concludes (output of verifier, input to report) ----------

class Verdict(str, Enum):
    VERIFIED = "verified"          # ✅
    DISCREPANCY = "discrepancy"    # ⚠️
    CONTRADICTED = "contradicted"  # ❌
    CANNOT_VERIFY = "cannot_verify"  # ❓


class Source(BaseModel):
    kind: str  # "crustdata_company_enrich", "crustdata_person_enrich", "web_search", ...
    ref: str   # URL, crustdata_company_id, or endpoint + query
    retrieved_at: str  # ISO timestamp


class CheckResult(BaseModel):
    claim: str  # human-readable claim, e.g. "Software Engineer at Acme, 2021-03 to 2023-06"
    check: str  # "employer_exists", "founded_before_start", "headcount_vs_team", "tenure", "title", ...
    verdict: Verdict
    reasons: list[str]
    sources: list[Source] = Field(default_factory=list)


class Referee(BaseModel):
    name: str
    profile_url: Optional[str] = None
    title_at_employer: Optional[str] = None
    overlap: str = ""  # e.g. "2021-05 to 2022-11 at Acme"
    draft_message: str = ""  # drafted only, NEVER sent


class VerificationReport(BaseModel):
    run_id: str
    candidate: str
    checks: list[CheckResult]
    referees: list[Referee] = Field(default_factory=list)
    decision: str  # "auto_clear" or "human_review"
    summary: str = ""
