"""Streamlit UI for the background-verification agent.

    streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from bgv.models import VerificationReport
from bgv.store import Store
from bgv.verifier import Verifier

ICON = {"verified": "✅ verified", "discrepancy": "⚠️ discrepancy", "contradicted": "❌ contradicted",
        "cannot_verify": "❓ cannot verify"}
ORDER = {"contradicted": 0, "discrepancy": 1, "cannot_verify": 2, "verified": 3}

st.set_page_config(page_title="Background Verification", page_icon="🔎", layout="wide")


@st.cache_resource
def get_store() -> Store:
    return Store()


def _link(ref: str) -> str:
    url = ref.split(" -> ")[-1].strip()
    return f"[{ref}]({url})" if url.startswith("http") else f"`{ref}`"


def render_report(report: VerificationReport, key: str) -> None:
    counts = {v: sum(c.verdict.value == v for c in report.checks) for v in ICON}
    if report.decision == "auto_clear":
        st.success(f"**Auto-clear**: every claim for {report.candidate} was verified.")
    else:
        st.warning(f"**Human review needed** for {report.candidate}.")
    cols = st.columns(4)
    for col, v in zip(cols, ICON):
        col.metric(ICON[v], counts[v])
    st.caption(report.summary)

    checks = sorted(report.checks, key=lambda c: ORDER[c.verdict.value])
    st.dataframe(
        [{"Verdict": ICON[c.verdict.value], "Claim": c.claim, "Check": c.check, "Reason": " ".join(c.reasons),
          "Sources": " | ".join(f"{s.kind}: {s.ref}" for s in c.sources)} for c in checks],
        width="stretch", hide_index=True,
        column_config={"Reason": st.column_config.TextColumn(width="large"),
                       "Sources": st.column_config.TextColumn(width="large")})

    st.subheader("Evidence")
    for i, c in enumerate(checks):
        with st.expander(f"{ICON[c.verdict.value]} · {c.check} · {c.claim}",
                         expanded=c.verdict.value in ("contradicted", "discrepancy")):
            for r in c.reasons:
                st.markdown(f"- {r}")
            if c.sources:
                st.markdown("**Sources**")
                for s in c.sources:
                    st.markdown(f"- {s.kind}: {_link(s.ref)} _(retrieved {s.retrieved_at})_")
            else:
                st.caption("No external source (the check could not run).")

    st.subheader(f"Possible referees ({len(report.referees)})")
    st.caption("Drafts only. Nothing is sent by this tool. Contact referees only after the candidate has "
               "consented to reference checks.")
    for i, r in enumerate(report.referees):
        with st.expander(f"{r.name} · {r.title_at_employer or ''} · {r.overlap}"):
            if r.profile_url:
                st.markdown(f"Profile: [{r.profile_url}]({r.profile_url})")
            st.text_area("Draft message (not sent)", r.draft_message, height=220, key=f"{key}-ref-{i}")


tab_verify, tab_queue = st.tabs(["Verify a resume", "Human-review queue"])

with tab_verify:
    st.header("Verify a resume")
    upload = st.file_uploader("Resume (PDF or text)", type=["pdf", "txt", "md"])
    pasted = st.text_area("…or paste the resume text", height=150)
    find_refs = st.checkbox("Find possible referees (uses extra Person Search credits)", value=True)

    if st.button("Run verification", type="primary", disabled=not (upload or pasted.strip())):
        source, name = (upload.getvalue(), upload.name) if upload else (pasted, "pasted text")
        with st.status("Verifying…", expanded=True) as status:
            try:
                verifier = Verifier(store=get_store(), find_refs=find_refs, progress=st.write)
                st.session_state["report"] = verifier.run(source, filename=name)
                status.update(label="Done", state="complete", expanded=False)
            except Exception as exc:  # parse/LLM failures; API failures are already cannot_verify
                status.update(label="Failed", state="error")
                st.error(f"Verification could not run: {exc}. Running it again continues where it stopped.")

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
