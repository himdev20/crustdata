"""Streamlit UI: resume in, editable claims, verification, report, human review queue.

Run with: streamlit run app.py
Set BGV_VERIFIER=real in .env to use bgv.verifier instead of the mock.
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from bgv.llm import LLMError
from bgv.models import EducationClaim, JobClaim, ParsedResume, VerificationReport
from bgv.report import to_html, to_markdown, with_summary
from bgv.resume_parser import ResumeParseError, parse_resume

load_dotenv()

VERIFIER_MODULES = {"mock": "bgv.mock_verifier", "real": "bgv.verifier"}
JOB_TEXT_COLUMNS = ["employer", "employer_domain", "title", "start", "end"]
JOB_COLUMNS = [*JOB_TEXT_COLUMNS, "is_current", "claimed_team_size", "raw_text"]
EDUCATION_TEXT_COLUMNS = ["school", "degree", "field"]
EDUCATION_COLUMNS = [*EDUCATION_TEXT_COLUMNS, "start_year", "end_year"]
YEAR_MONTH_PATTERN = r"^(19|20)\d{2}-(0[1-9]|1[0-2])$"


# ---------- Verifier setting ----------

def verifier_name() -> str:
    return os.getenv("BGV_VERIFIER", "mock").strip().lower()


def load_verify():
    """Return verify() for the configured verifier. Raises ValueError or ImportError."""
    name = verifier_name()
    if name not in VERIFIER_MODULES:
        raise ValueError(f"BGV_VERIFIER must be one of {sorted(VERIFIER_MODULES)}, not {name!r}.")
    return importlib.import_module(VERIFIER_MODULES[name]).verify


# ---------- Converting claims to and from the edit tables ----------

def jobs_to_frame(jobs: list[JobClaim]) -> pd.DataFrame:
    frame = pd.DataFrame([job.model_dump() for job in jobs], columns=JOB_COLUMNS)
    frame[[*JOB_TEXT_COLUMNS, "raw_text"]] = frame[[*JOB_TEXT_COLUMNS, "raw_text"]].astype("string")
    frame["is_current"] = frame["is_current"].astype("boolean").fillna(False)
    frame["claimed_team_size"] = pd.to_numeric(frame["claimed_team_size"]).astype("Int64")
    return frame


def education_to_frame(education: list[EducationClaim]) -> pd.DataFrame:
    frame = pd.DataFrame([item.model_dump() for item in education], columns=EDUCATION_COLUMNS)
    frame[EDUCATION_TEXT_COLUMNS] = frame[EDUCATION_TEXT_COLUMNS].astype("string")
    for column in ("start_year", "end_year"):
        frame[column] = pd.to_numeric(frame[column]).astype("Int64")
    return frame


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Frame rows as dicts, with empty cells and blank text as None."""
    rows = []
    for row in frame.to_dict("records"):
        cleaned = {}
        for key, value in row.items():
            if pd.isna(value) or (isinstance(value, str) and not value.strip()):
                cleaned[key] = None
            else:
                cleaned[key] = value.strip() if isinstance(value, str) else value
        rows.append(cleaned)
    return rows


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def frame_to_jobs(frame: pd.DataFrame) -> list[JobClaim]:
    jobs = []
    for row in _records(frame):
        if not row["employer"] and not row["title"]:
            continue  # a blank row added in the table
        is_current = bool(row["is_current"])
        jobs.append(
            JobClaim(
                employer=row["employer"] or "",
                employer_domain=row["employer_domain"],
                title=row["title"] or "",
                start=row["start"],
                end=None if is_current else row["end"],
                is_current=is_current,
                claimed_team_size=_optional_int(row["claimed_team_size"]),
                raw_text=row["raw_text"] or "",
            )
        )
    return jobs


def frame_to_education(frame: pd.DataFrame) -> list[EducationClaim]:
    return [
        EducationClaim(
            school=row["school"],
            degree=row["degree"],
            field=row["field"],
            start_year=_optional_int(row["start_year"]),
            end_year=_optional_int(row["end_year"]),
        )
        for row in _records(frame)
        if row["school"]
    ]


# ---------- Review queue storage ----------

def queue_path() -> Path:
    # Outside the repo by default: this file holds candidate data.
    default = Path.home() / ".bgv" / "review_queue.json"
    return Path(os.getenv("BGV_REVIEW_FILE", default))


def load_queue() -> dict[str, Any]:
    path = queue_path()
    return json.loads(path.read_text()) if path.exists() else {}


def _save_queue(queue: dict[str, Any]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename, so a crash mid-write can't corrupt recorded decisions.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(queue, handle, indent=2)
    os.replace(handle.name, path)


def enqueue_run(report: VerificationReport, verifier: str) -> None:
    queue = load_queue()
    queue[report.run_id] = {
        "report": report.model_dump(mode="json"),
        "verifier": verifier,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review": None,
    }
    _save_queue(queue)


def record_review(run_id: str, decision: str, reviewer: str) -> None:
    """Record a decision once. A recorded decision is never overwritten."""
    queue = load_queue()
    if queue[run_id]["review"] is not None:
        raise ValueError(f"Run {run_id} has already been reviewed.")
    queue[run_id]["review"] = {
        "decision": decision,
        "reviewer": reviewer,
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_queue(queue)


# ---------- Shared display ----------

def show_report(report: VerificationReport, key: str) -> None:
    markdown = to_markdown(report)
    st.markdown(markdown)
    left, right = st.columns(2)
    left.download_button(
        "Download report (Markdown)",
        markdown,
        file_name=f"bgv-report-{report.run_id}.md",
        mime="text/markdown",
        key=f"download_md_{key}",
    )
    right.download_button(
        "Download report (HTML)",
        to_html(report),
        file_name=f"bgv-report-{report.run_id}.html",
        mime="text/html",
        key=f"download_html_{key}",
    )


def show_mock_warning(verifier: str) -> None:
    if verifier == "mock":
        st.warning("Mock verifier: every verdict here is made up. Don't use it to make a decision.")


# ---------- Verify page ----------

def reset_check() -> None:
    for key in ("resume", "report", "report_verifier", "jobs_frame", "education_frame"):
        st.session_state.pop(key, None)
    st.session_state["parse_count"] = st.session_state.get("parse_count", 0) + 1


def input_step() -> None:
    st.subheader("1. Add a resume")
    mode = st.radio("Resume source", ["Paste text", "Upload a PDF"], horizontal=True, key="source_mode")
    if mode == "Upload a PDF":
        upload = st.file_uploader("Resume PDF", type=["pdf"], key="pdf_upload")
        source, is_pdf = (upload.getvalue() if upload else None), True
    else:
        source, is_pdf = st.text_area("Resume text", height=260, key="resume_text"), False

    if not st.button("Read resume", type="primary", key="parse"):
        return
    if not source:
        st.error("Add a resume first.")
        return
    try:
        with st.spinner("Reading the resume..."):
            resume = parse_resume(source, is_pdf=is_pdf)
    except (ResumeParseError, LLMError) as exc:
        st.error(str(exc))
        return
    st.session_state["resume"] = resume
    st.session_state["jobs_frame"] = jobs_to_frame(resume.jobs)
    st.session_state["education_frame"] = education_to_frame(resume.education)
    st.rerun()


def edit_step() -> None:
    st.subheader("2. Check the claims")
    st.caption("Correct anything the parser got wrong. Verification uses what's shown here.")
    resume = st.session_state["resume"]
    version = st.session_state.get("parse_count", 0)

    left, right = st.columns(2)
    full_name = left.text_input("Full name", resume.full_name, key=f"full_name_{version}")
    email = right.text_input("Email", resume.email or "", key=f"email_{version}")
    linkedin = left.text_input("LinkedIn URL", resume.linkedin_url or "", key=f"linkedin_{version}")
    location = right.text_input("Location", resume.location or "", key=f"location_{version}")

    st.markdown("**Jobs**")
    jobs = st.data_editor(
        st.session_state["jobs_frame"],
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key=f"jobs_editor_{version}",
        column_config={
            "employer": st.column_config.TextColumn("Employer"),
            "employer_domain": st.column_config.TextColumn("Domain"),
            "title": st.column_config.TextColumn("Title"),
            "start": st.column_config.TextColumn("Start (YYYY-MM)", validate=YEAR_MONTH_PATTERN),
            "end": st.column_config.TextColumn("End (YYYY-MM)", validate=YEAR_MONTH_PATTERN),
            "is_current": st.column_config.CheckboxColumn("Current"),
            "claimed_team_size": st.column_config.NumberColumn("Team size", min_value=1, step=1),
            "raw_text": st.column_config.TextColumn("Resume line"),
        },
    )
    st.markdown("**Education**")
    education = st.data_editor(
        st.session_state["education_frame"],
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key=f"education_editor_{version}",
        column_config={
            "school": st.column_config.TextColumn("School"),
            "degree": st.column_config.TextColumn("Degree"),
            "field": st.column_config.TextColumn("Field"),
            "start_year": st.column_config.NumberColumn("Start year", format="%d", step=1),
            "end_year": st.column_config.NumberColumn("End year", format="%d", step=1),
        },
    )

    edited = ParsedResume(
        full_name=full_name.strip(),
        email=email.strip() or None,
        linkedin_url=linkedin.strip() or None,
        location=location.strip() or None,
        jobs=frame_to_jobs(jobs),
        education=frame_to_education(education),
    )
    if not edited.full_name:
        st.warning("No candidate name. Add one if you can, since person checks depend on it.")

    if st.button("Run verification", type="primary", key="verify"):
        run_verification(edited)
    st.button("Start over", key="restart_from_edit", on_click=reset_check)


def run_verification(resume: ParsedResume) -> None:
    name = verifier_name()
    try:
        verify = load_verify()
        with st.spinner("Verifying claims..."):
            report = with_summary(verify(resume))
    except Exception as exc:  # A verifier failure shouldn't lose the reviewer's edits.
        st.error(f"Verification failed: {exc}")
        return
    if report.decision == "human_review":
        enqueue_run(report, name)
    st.session_state["report"] = report
    st.session_state["report_verifier"] = name
    st.rerun()


def report_step() -> None:
    st.subheader("3. Report")
    report = st.session_state["report"]
    show_mock_warning(st.session_state["report_verifier"])
    show_report(report, key="current")
    if report.decision == "human_review":
        st.info("This run is in the Human review queue.")
    st.button("Check another resume", key="restart_from_report", on_click=reset_check)


def verify_page() -> None:
    st.title("Background verification")
    show_mock_warning(verifier_name())
    if "report" in st.session_state:
        report_step()
    elif "resume" in st.session_state:
        edit_step()
    else:
        input_step()


# ---------- Review page ----------

def review_page() -> None:
    st.title("Human review queue")
    queue = load_queue()
    runs = sorted(queue.items(), key=lambda item: item[1]["created_at"], reverse=True)
    pending = [item for item in runs if item[1]["review"] is None]
    decided = [item for item in runs if item[1]["review"] is not None]
    st.caption(f"{len(pending)} waiting, {len(decided)} decided.")

    reviewer = st.text_input("Your name (recorded with each decision)", key="reviewer").strip()

    st.subheader("Waiting for review")
    if not pending:
        st.write("Nothing is waiting for review.")
    for run_id, run in pending:
        report = VerificationReport.model_validate(run["report"])
        with st.expander(f"{report.candidate} ({run_id})"):
            show_mock_warning(run["verifier"])
            show_report(report, key=run_id)
            left, right = st.columns(2)
            approved = left.button("Approve", key=f"approve_{run_id}", type="primary")
            rejected = right.button("Reject", key=f"reject_{run_id}")
            if approved or rejected:
                if not reviewer:
                    st.error("Enter your name first.")
                else:
                    record_review(run_id, "approved" if approved else "rejected", reviewer)
                    st.rerun()

    st.subheader("Decided")
    if not decided:
        st.write("No decisions yet.")
    for run_id, run in decided:
        report = VerificationReport.model_validate(run["report"])
        review = run["review"]
        label = f"{report.candidate} ({run_id}): {review['decision']} by {review['reviewer']}"
        with st.expander(label):
            st.caption(f"Decided {review['decided_at']}")
            show_mock_warning(run["verifier"])
            show_report(report, key=run_id)


def main() -> None:
    st.set_page_config(page_title="Background verification", layout="wide")
    page = st.sidebar.radio("Page", ["Verify a candidate", "Human review queue"], key="page")
    if page == "Verify a candidate":
        verify_page()
    else:
        review_page()


if __name__ == "__main__":
    main()
