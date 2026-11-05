"""Orchestrates a verification run: parse -> company checks -> person checks -> referees -> report.

Resumable: every step's output is saved to SQLite (store.steps). A rerun with the same
resume gets the same run_id and skips steps that already finished cleanly; steps that hit
an API error are saved as not-ok and are retried. Crustdata responses are also cached by
the client, so a rerun does not spend credits twice.

Decision: "auto_clear" only when every check is verified. Anything else (discrepancy,
contradicted, or cannot_verify, which includes API failures) goes to "human_review".
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from pathlib import Path
from typing import Callable

from .company_checks import CompanyResult, check_company
from .crustdata_client import CrustdataClient
from .models import CheckResult, ParsedResume, Referee, Verdict, VerificationReport
from .person_checks import PersonResult, check_person
from .referees import find_referees
from .resume_parser import extract_text, parse_resume
from .store import Store

log = logging.getLogger("bgv")
Progress = Callable[[str], None]


def make_run_id(resume_text: str) -> str:
    normalized = " ".join(resume_text.split()).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def candidate_key(resume: ParsedResume) -> str:
    """Stable id for 'the same person' across resumes (email, else LinkedIn, else name)."""
    basis = (resume.email or resume.linkedin_url or resume.full_name).strip().lower()
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def decide(checks: list[CheckResult]) -> str:
    if checks and all(c.verdict == Verdict.VERIFIED for c in checks):
        return "auto_clear"
    return "human_review"


def summarize(resume: ParsedResume, checks: list[CheckResult], decision: str, notes: list[str]) -> str:
    counts = Counter(c.verdict.value for c in checks)
    parts = [f"{resume.full_name}: {len(resume.jobs)} job(s), {len(checks)} checks - "
             + ", ".join(f"{counts.get(v.value, 0)} {v.value}" for v in Verdict) + "."]
    flagged = [c for c in checks if c.verdict in (Verdict.CONTRADICTED, Verdict.DISCREPANCY)]
    if flagged:
        parts.append("Flagged: " + "; ".join(f"{c.check} ({c.claim})" for c in flagged[:5]) + ".")
    if decision == "human_review" and not flagged:
        parts.append("Nothing contradicted, but some claims could not be verified.")
    parts.extend(notes)
    return " ".join(parts)


class Verifier:
    def __init__(self, client: CrustdataClient | None = None, store: Store | None = None,
                 find_refs: bool = True, progress: Progress | None = None):
        self._client = client
        self.store = store or Store()
        self.find_refs = find_refs
        self.progress = progress or (lambda msg: log.info(msg))

    @property
    def client(self) -> CrustdataClient:
        if self._client is None:  # created lazily so a missing key fails as cannot_verify, not a crash
            self._client = CrustdataClient()
        return self._client

    def _step(self, run_id: str, key: str, fn, load, dump, ok=lambda out: True):
        cached = self.store.load_step(run_id, key)
        if cached is not None:
            self.progress(f"{key}: loaded from checkpoint")
            return load(cached)
        self.progress(f"{key}: running")
        out = fn()
        self.store.save_step(run_id, key, dump(out), ok=ok(out))
        return out

    def run(self, source: str | Path | bytes, filename: str | None = None) -> VerificationReport:
        text = extract_text(source, filename)
        run_id = make_run_id(text)
        self.store.start_run(run_id, filename or (str(source) if isinstance(source, Path) else ""))
        try:
            report = self._run(run_id, text)
        except Exception as exc:
            self.store.update_run(run_id, status=f"failed: {exc}"[:500])
            raise
        prev = self.store.get_run(run_id) or {}
        review = "not_needed" if report.decision == "auto_clear" else (
            prev.get("review_status") if prev.get("review_status") in ("approved", "rejected") else "pending")
        self.store.update_run(run_id, candidate=report.candidate, status="done", decision=report.decision,
                              review_status=review, report_json=report.model_dump_json())
        return report

    def _run(self, run_id: str, text: str) -> VerificationReport:
        resume: ParsedResume = self._step(
            run_id, "parse", lambda: parse_resume(text),
            load=ParsedResume.model_validate, dump=lambda r: r.model_dump(mode="json"))
        ckey = candidate_key(resume)
        self.store.update_run(run_id, candidate=resume.full_name, candidate_key=ckey)

        client_error = self._client_error()

        companies: dict[int, CompanyResult] = {}
        for i, job in enumerate(resume.jobs):
            if client_error:
                companies[i] = _failed_company(i, job, client_error)
                continue
            companies[i] = self._step(
                run_id, f"company:{i}", lambda job=job, i=i: check_company(self.client, job, i, self.store, ckey, run_id),
                load=CompanyResult.model_validate, dump=lambda r: r.model_dump(mode="json"),
                ok=lambda r: not r.api_errors)

        if client_error:
            person = _failed_person(resume, client_error)
        else:
            person = self._step(
                run_id, "person", lambda: check_person(self.client, resume, companies),
                load=PersonResult.model_validate, dump=lambda r: r.model_dump(mode="json"),
                ok=lambda r: not r.api_errors)

        refs: list[Referee] = []
        notes: list[str] = []
        if self.find_refs and not client_error and resume.jobs:
            refs, notes = self._step(
                run_id, "referees", lambda: find_referees(self.client, resume, companies, person),
                load=lambda d: ([Referee.model_validate(r) for r in d["referees"]], d["notes"]),
                dump=lambda out: {"referees": [r.model_dump() for r in out[0]], "notes": out[1]},
                ok=lambda out: not any("failed" in n for n in out[1]))
        elif client_error:
            notes = [f"Referee search skipped: {client_error}"]

        checks = [c for i in sorted(companies) for c in companies[i].checks] + person.checks
        if not resume.jobs:
            notes.append("The resume lists no jobs, so there was nothing to verify.")
        decision = decide(checks)
        return VerificationReport(run_id=run_id, candidate=resume.full_name, checks=checks, referees=refs,
                                  decision=decision, summary=summarize(resume, checks, decision, notes))

    def _client_error(self) -> str | None:
        try:
            _ = self.client
            return None
        except Exception as exc:
            return f"Crustdata client unavailable: {exc}"


def _failed_company(i, job, reason) -> CompanyResult:
    from .company_checks import job_label

    return CompanyResult(job_index=i, employer=job.employer, api_errors=[reason], checks=[
        CheckResult(claim=job_label(job), check="employer_exists", verdict=Verdict.CANNOT_VERIFY, reasons=[reason])])


def _failed_person(resume: ParsedResume, reason) -> PersonResult:
    from .company_checks import job_label

    return PersonResult(api_errors=[reason], checks=[
        CheckResult(claim=job_label(j), check="employment_on_profile", verdict=Verdict.CANNOT_VERIFY, reasons=[reason])
        for j in resume.jobs])


def verify(source: str | Path | bytes, filename: str | None = None, **kwargs) -> VerificationReport:
    return Verifier(**kwargs).run(source, filename)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rep = verify(Path(sys.argv[1]))
    icon = {"verified": "OK ", "discrepancy": "WARN", "contradicted": "FAIL", "cannot_verify": "??  "}
    print(f"\nRun {rep.run_id} - {rep.candidate} - decision: {rep.decision}\n{rep.summary}\n")
    for c in rep.checks:
        print(f"[{icon[c.verdict.value]}] {c.check:22} {c.claim}\n        {c.reasons[0]}")
        for s in c.sources[:2]:
            print(f"        source: {s.kind}: {s.ref[:100]}")
    for r in rep.referees:
        print(f"Referee (draft, not sent): {r.name} - {r.title_at_employer} - {r.overlap}")
