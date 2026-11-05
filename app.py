"""Streamlit UI for the background-verification agent.

    streamlit run app.py
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

import streamlit as st

from bgv import report as report_fmt
from bgv.company_checks import job_label
from bgv.models import CheckResult, ParsedResume, Source, Verdict, VerificationReport
from bgv.store import Store
from bgv.verifier import Verifier

SAMPLES_DIR = Path(__file__).parent / "samples"

# Label, badge color. Most serious first: this also orders checks within an employer.
VERDICTS = {
    Verdict.CONTRADICTED: ("Contradicted", "❌", "red"),
    Verdict.DISCREPANCY: ("Discrepancy", "⚠️", "orange"),
    Verdict.CANNOT_VERIFY: ("Cannot verify", "❓", "gray"),
    Verdict.VERIFIED: ("Verified", "✅", "green"),
}
TILE_ORDER = (Verdict.VERIFIED, Verdict.DISCREPANCY, Verdict.CONTRADICTED, Verdict.CANNOT_VERIFY)
CHECK_LABELS = {
    "employer_exists": "Employer exists",
    "founded_before_start": "Founded before the start date",
    "headcount_vs_team": "Claimed team size vs headcount",
    "repeat_low_footprint_employer": "Low-footprint employer seen on other resumes",
    "employment_on_profile": "Job appears on their profile",
    "tenure": "Employment dates",
    "title": "Job title",
}
SOURCE_LABELS = {
    "crustdata_company_identify": "Crustdata company lookup",
    "crustdata_company_enrich": "Crustdata company profile",
    "crustdata_person_enrich": "Crustdata person profile",
    "crustdata_person_search": "Crustdata person search",
    "web_search": "Web search",
    "bgv_history": "Earlier verification run",
}
OTHER_GROUP = "Other checks"

# (the verifier's progress key, what the user sees). "report" is this app's own step.
STAGES = (
    ("parse", "Reading resume"),
    ("company", "Checking employers"),
    ("person", "Checking career history"),
    ("referees", "Finding referees"),
    ("report", "Writing report"),
)
STAGE_ICONS = {"pending": "⬜", "running": "⏳", "done": "✅", "skipped": "➖", "failed": "❌"}

st.set_page_config(page_title="Background Verification", page_icon="🔎", layout="wide")


@st.cache_resource
def get_store() -> Store:
    return Store()


@st.cache_resource
def get_summaries() -> dict[str, str]:
    """Shared across page reloads, so reopening a report doesn't wait on another LLM call."""
    return {}


def plain(text: str) -> str:
    """Make text safe for st.markdown: resume content must not become formatting or links."""
    flattened = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"([\\`*_\[\]<>|~#$:])", r"\\\1", flattened)


# ---------- Progress ----------

class StageTracker:
    """Shows each stage's state inside st.status, driven by the verifier's progress messages.

    The verifier sends "parse: running", "company:2: running", "person: loaded from
    checkpoint" and so on. A stage ends when a later one starts, and any stage jumped
    over (no jobs on the resume, referee search turned off) is shown as skipped.
    """

    def __init__(self, status) -> None:
        self.status = status
        self.states = ["pending"] * len(STAGES)
        self.details = [""] * len(STAGES)
        self.current = -1
        self.box = st.empty()

    def on_progress(self, message: str) -> None:
        parts = message.split(":")
        if parts[0] not in {key for key, _ in STAGES}:
            return  # an unfamiliar message shouldn't break a verification
        detail = ""
        if parts[0] == "company" and len(parts) > 1 and parts[1].isdigit():
            detail = f"job {int(parts[1]) + 1}"
        self.start(parts[0], detail)

    def start(self, key: str, detail: str = "") -> None:
        index = next(i for i, (stage, _) in enumerate(STAGES) if stage == key)
        if index > self.current:
            if self.current >= 0:
                self.states[self.current] = "done"
                self.details[self.current] = ""
            for skipped in range(self.current + 1, index):
                self.states[skipped] = "skipped"
            self.current = index
            self.states[index] = "running"
        self.details[index] = detail
        self.status.update(label=f"{STAGES[index][1]}…", state="running")
        self._render()

    def finish(self) -> None:
        self.states[self.current] = "done"
        self.details[self.current] = ""
        self._render()

    def fail(self) -> None:
        if self.current >= 0:
            self.states[self.current] = "failed"
        self._render()

    def _render(self) -> None:
        lines = []
        for (_, label), state, detail in zip(STAGES, self.states, self.details):
            suffix = f" ({detail})" if detail else " (skipped)" if state == "skipped" else ""
            lines.append(f"{STAGE_ICONS[state]} {label}{suffix}")
        self.box.markdown("  \n".join(lines))


# ---------- Report ----------

def summarized(report: VerificationReport) -> VerificationReport:
    """Make the LLM call once per set of results, not on every Streamlit rerun.

    run_id comes from the resume text, so a rerun after an API failure has the same
    run_id but new verdicts. The key includes the results so its summary can't go stale.
    """
    digest = hashlib.sha256(report.model_dump_json().encode()).hexdigest()
    cache = get_summaries()
    if digest not in cache:
        cache[digest] = report_fmt.with_summary(report).summary
    return report.model_copy(update={"summary": cache[digest]})


def download_buttons(report: VerificationReport, key: str) -> None:
    name = f"bgv_{report.run_id}"
    c1, c2, _ = st.columns([1, 1, 4])
    c1.download_button("Download Markdown", report_fmt.to_markdown(report), file_name=f"{name}.md",
                       mime="text/markdown", key=f"{key}-md")
    c2.download_button("Download HTML", report_fmt.to_html(report), file_name=f"{name}.html",
                       mime="text/html", key=f"{key}-html")


def decision_banner(report: VerificationReport) -> None:
    flagged = sum(c.verdict != Verdict.VERIFIED for c in report.checks)
    candidate = html.escape(report.candidate)
    if report.decision == "auto_clear":
        background, color, title = "#1a7f37", "#ffffff", "Auto-clear"
        detail = f"All {len(report.checks)} checks verified for {candidate}."
    else:  # Anything unrecognised goes to a person.
        background, color, title = "#f5a623", "#1f2328", "Human review"
        detail = (f"{flagged} of {len(report.checks)} checks need a reviewer for {candidate}."
                  if report.checks else f"No checks could be run for {candidate}.")
    st.html(
        f'<div role="status" style="background:{background};color:{color};padding:20px 24px;'
        f'border-radius:8px;margin-bottom:8px">'
        f'<div style="font-size:2rem;font-weight:700;line-height:1.2">{title}</div>'
        f'<div style="font-size:1.05rem;margin-top:4px">{detail}</div></div>'
    )


def verdict_tiles(report: VerificationReport) -> None:
    for column, verdict in zip(st.columns(4), TILE_ORDER):
        label, icon, _ = VERDICTS[verdict]
        count = sum(c.verdict == verdict for c in report.checks)
        column.metric(f"{icon} {label}", count, border=True)


def employers_by_claim(run_id: str) -> dict[str, str]:
    """Map each check's claim to its employer, using the jobs saved when the resume was parsed."""
    saved = get_store().load_step(run_id, "parse")
    if saved is None:
        return {}
    return {job_label(job): job.employer for job in ParsedResume.model_validate(saved).jobs}


def group_by_employer(report: VerificationReport) -> dict[str, dict[str, list[CheckResult]]]:
    """Return {employer: {claim: checks}} in resume order, with unmatched checks last."""
    employers = employers_by_claim(report.run_id)
    display_names: dict[str, str] = {}
    groups: dict[str, dict[str, list[CheckResult]]] = {}
    other: dict[str, list[CheckResult]] = {}
    for check in report.checks:
        employer = employers.get(check.claim)
        if not employer:
            other.setdefault(check.claim, []).append(check)
            continue
        name = display_names.setdefault(employer.casefold(), employer)
        groups.setdefault(name, {}).setdefault(check.claim, []).append(check)
    if other:
        groups[OTHER_GROUP] = other
    return groups


def source_line(source: Source) -> str:
    """Return one Markdown line for a source. Only an http(s) URL in its ref becomes a link."""
    kind = plain(SOURCE_LABELS.get(source.kind, source.kind.replace("_", " ").capitalize()))
    match = re.search(r"https?://\S+", source.ref)
    if not match:
        return f"- {kind}: {plain(source.ref)}"
    url = match.group().replace("(", "%28").replace(")", "%29")
    label = plain(source.ref[:match.start()].rstrip(" ->:")) or plain(url)
    return f"- {kind}: [{label}]({url})"


def render_check(check: CheckResult) -> None:
    label, icon, color = VERDICTS[check.verdict]
    name = CHECK_LABELS.get(check.check, check.check.replace("_", " ").capitalize())
    badge_column, text_column = st.columns([1, 5])
    with badge_column:
        st.badge(label, icon=icon, color=color)
    with text_column:
        st.markdown(f"**{plain(name)}**")
        for reason in check.reasons or ["No reason recorded."]:
            st.markdown(plain(reason))
        with st.expander("Evidence"):
            if check.sources:
                st.markdown("\n".join(source_line(source) for source in check.sources))
            else:
                st.caption("No sources recorded for this check.")


def render_checks(report: VerificationReport) -> None:
    st.subheader("Checks by employer")
    if not report.checks:
        st.info("No checks were run.")
    severity = list(VERDICTS)
    for employer, claims in group_by_employer(report).items():
        with st.container(border=True):
            st.markdown(f"#### {plain(employer)}")
            for claim, checks in claims.items():
                st.caption(plain(claim))
                for check in sorted(checks, key=lambda c: severity.index(c.verdict)):
                    render_check(check)


def render_referees(report: VerificationReport) -> None:
    st.subheader(f"Referees ({len(report.referees)})")
    st.caption("Nothing is sent by this tool. Contact referees only after the candidate has "
               "consented to reference checks.")
    if not report.referees:
        st.info("No referees found.")
    for referee in report.referees:
        with st.container(border=True):
            details = " · ".join(plain(v) for v in (referee.title_at_employer, referee.overlap) if v)
            url = referee.profile_url or ""
            name = plain(referee.name)
            st.markdown(f"**[{name}]({url})**" if url.startswith(("http://", "https://")) else f"**{name}**")
            if details:
                st.caption(details)
            st.badge(report_fmt.DRAFT_LABEL, icon="✉️", color="orange")
            st.code(referee.draft_message, language=None, wrap_lines=True)


def render_report(report: VerificationReport, key: str) -> None:
    report = summarized(report)
    decision_banner(report)
    verdict_tiles(report)
    st.markdown(plain(report.summary))
    download_buttons(report, key)
    render_checks(report)
    render_referees(report)


# ---------- Verify tab ----------

def sample_files() -> list[str]:
    return sorted(path.name for path in SAMPLES_DIR.glob("*.txt"))


def load_sample() -> None:
    """Fill the text box. Also clears any upload, which would otherwise be verified instead."""
    choice = st.session_state.get("sample_choice")
    if choice:
        st.session_state["resume_text"] = (SAMPLES_DIR / choice).read_text()
        st.session_state["upload_version"] = st.session_state.get("upload_version", 0) + 1


def run_verification(source: str | bytes, name: str, find_refs: bool) -> None:
    failure = None
    with st.status("Reading resume…", expanded=True) as status:
        tracker = StageTracker(status)
        tracker.start("parse")
        try:
            verifier = Verifier(store=get_store(), find_refs=find_refs, progress=tracker.on_progress)
            report = verifier.run(source, filename=name)
            tracker.start("report")
            summarized(report)
            tracker.finish()
            st.session_state["report"] = report
            status.update(label="Verification complete", state="complete", expanded=False)
        except Exception as exc:  # parse/LLM failures; API failures are already cannot_verify
            tracker.fail()
            status.update(label="Verification failed", state="error", expanded=True)
            failure = str(exc)
    # Outside the status box, so the reason stays visible even if the box collapses.
    if failure:
        st.error(f"Verification could not run: {plain(failure)}. "
                 "Running it again continues where it stopped.")


tab_verify, tab_queue = st.tabs(["Verify a resume", "Human-review queue"])

with tab_verify:
    st.header("Verify a resume")
    upload_version = st.session_state.get("upload_version", 0)
    upload = st.file_uploader("Resume (PDF or text)", type=["pdf", "txt", "md"], key=f"upload_{upload_version}")
    samples = sample_files()
    if samples:
        st.selectbox("Load sample", samples, index=None, placeholder="Choose a sample resume",
                     key="sample_choice", on_change=load_sample)
    pasted = st.text_area("…or paste the resume text", height=150, key="resume_text")
    find_refs = st.checkbox("Find possible referees (uses extra Person Search credits)", value=True)

    if st.button("Run verification", type="primary", disabled=not (upload or pasted.strip())):
        st.session_state.pop("report", None)
        source, name = (upload.getvalue(), upload.name) if upload else (pasted, "pasted text")
        run_verification(source, name, find_refs)

    if "report" in st.session_state:
        render_report(st.session_state["report"], key="current")

with tab_queue:
    st.header("Human-review queue")
    store = get_store()
    pending = store.list_runs("pending")
    if not pending:
        st.info("Nothing waiting for review.")
    else:
        labels = {f"{r['candidate']} · {r['updated_at']} · {r['run_id']}": r for r in pending}
        choice = st.selectbox(f"{len(pending)} run(s) waiting", list(labels))
        run = labels[choice]
        report = VerificationReport.model_validate_json(run["report_json"])
        render_report(report, key=f"q-{run['run_id']}")
        notes = st.text_area("Reviewer notes", run.get("reviewer_notes") or "", key=f"notes-{run['run_id']}")
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("Approve", type="primary", key=f"ok-{run['run_id']}"):
            store.update_run(run["run_id"], review_status="approved", reviewer_notes=notes)
            st.rerun()
        if c2.button("Reject", key=f"no-{run['run_id']}"):
            store.update_run(run["run_id"], review_status="rejected", reviewer_notes=notes)
            st.rerun()

    with st.expander("Reviewed and auto-cleared runs"):
        done = [r for r in store.list_runs() if r["review_status"] in ("approved", "rejected", "not_needed")]
        st.dataframe([{"Candidate": r["candidate"], "Decision": r["decision"], "Review": r["review_status"],
                       "Notes": r["reviewer_notes"], "Updated": r["updated_at"], "Run": r["run_id"]} for r in done],
                     width="stretch", hide_index=True)
