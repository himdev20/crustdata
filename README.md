# Background-verification agent (Indian hiring)

Takes a resume (PDF or text) and produces a source-cited report with a verdict on each claim:
`verified`, `discrepancy`, `contradicted` or `cannot_verify`. Runs that are not fully verified go to a
human-review queue.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
```

`.env` (never commit it):

```
CRUSTDATA_API_KEY=...
GEMINI_API_KEY=...
# optional: GEMINI_MODEL, GEMINI_FALLBACK_MODELS, GEMINI_TIMEOUT_S, BGV_DB_PATH, BGV_CACHE_PATH
```

## Run

```bash
streamlit run app.py                                      # UI: upload, report, review queue
python -m bgv.verifier tests/fixtures/sample_resume.txt   # CLI
python -m pytest tests                                    # offline tests (fake APIs, no credits)
```

## Project structure

| File | Role |
|---|---|
| `bgv/models.py` | Shared data contract (claims, verdicts, report) |
| `bgv/crustdata_client.py` | Crustdata REST client with retry and SQLite response cache |
| `bgv/llm.py` | `complete_json(prompt, schema)`; Gemini today, provider is swappable |
| `bgv/resume_parser.py` | PDF/text -> `ParsedResume` (LLM + deterministic clean-up) |
| `bgv/company_checks.py` | Identify -> Enrich -> Web Search per employer |
| `bgv/person_checks.py` | Person Enrich (LinkedIn URL) or Person Search; tenure and title checks |
| `bgv/referees.py` | Colleagues with overlapping tenure + draft outreach (never sent) |
| `bgv/verifier.py` | Runs everything, checkpoints each step, decides `auto_clear` / `human_review` |
| `bgv/store.py` | SQLite: run checkpoints, review queue, employer sightings |
| `bgv/utils.py` | Name normalisation, fuzzy matching, month arithmetic |
| `app.py` | Streamlit UI |

## Verdict rules

| Check | Rule |
|---|---|
| `employer_exists` | verified if found in Crustdata; contradicted if not found **and** no web footprint |
| `founded_before_start` | contradicted if the company was founded after the claimed start year |
| `headcount_vs_team` | discrepancy if the claimed team is larger than the headcount during the tenure |
| `repeat_low_footprint_employer` | discrepancy if a low-footprint employer is on 2+ other candidates' resumes |
| `tenure` | verified if start and end are within ±3 months of the profile, else discrepancy |
| `title` | verified if similar to a title held at that employer, else discrepancy |
| `employment_on_profile` | cannot_verify when the job is missing from the profile (never contradicted) |

Every API failure becomes `cannot_verify` with the error as the reason. The decision is `auto_clear` only if
**every** check is verified, so unverifiable claims (including API outages) always reach a human.

## Data and privacy

`*.sqlite` files hold candidate data and API responses and are git-ignored. Referee messages are drafts:
nothing in this project sends messages. Contact referees only after the candidate consents.
