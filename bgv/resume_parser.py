"""Resume (PDF or text) -> ParsedResume.

The LLM does the reading; everything it returns is validated by pydantic and then
normalised by plain code (dates, LinkedIn URL, current-job flags), so the checks
downstream never depend on the model's formatting habits.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader

from .llm import complete_json
from .models import ParsedResume

MAX_CHARS = 30_000  # resumes are short; this guards against pasting a whole book

PROMPT = """You extract structured data from an Indian job candidate's resume for background verification.

Rules:
- Copy facts exactly as the resume states them. Never guess or fill gaps.
- One entry in "jobs" per role. If the candidate held several titles at one employer, make one entry per title.
- employer: the company name as written (keep suffixes like "Pvt Ltd").
- employer_domain: only if the resume shows the company's website or an email at that company; else null.
- start / end: "YYYY-MM". If only a year is given, use "YYYY". If the role is current ("Present", "Till date", "Current"),
  set is_current=true and end=null. Unknown -> null.
- claimed_team_size: only if the resume claims a team size for that role ("led a team of 15" -> 15); else null.
- raw_text: the exact resume line(s) that describe the role (employer, title and dates), for citation.
- linkedin_url: the full LinkedIn profile URL if present.
- education: school, degree, field, start_year, end_year when stated.

Resume text:
<<<
{text}
>>>"""

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


class ResumeParseError(Exception):
    pass


# ---------- text extraction ----------

def extract_text(source: str | Path | bytes, filename: str | None = None) -> str:
    """Accepts a path to .pdf/.txt, raw PDF/text bytes, or resume text itself."""
    if isinstance(source, bytes):
        if source[:5] == b"%PDF-" or (filename or "").lower().endswith(".pdf"):
            return _pdf_text(source)
        return source.decode("utf-8", errors="replace")

    if isinstance(source, Path) or (isinstance(source, str) and len(source) < 500 and "\n" not in source
                                    and Path(source).suffix.lower() in {".pdf", ".txt", ".md"}):
        path = Path(source)
        if not path.exists():
            raise ResumeParseError(f"File not found: {path}")
        data = path.read_bytes()
        return _pdf_text(data) if path.suffix.lower() == ".pdf" else data.decode("utf-8", errors="replace")

    return str(source)


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise ResumeParseError(f"Could not read PDF: {exc}") from exc
    if not text.strip():
        raise ResumeParseError("The PDF has no extractable text (it may be a scanned image; OCR is not supported).")
    # pypdf hides link targets (e.g. a LinkedIn icon); add them so the URL is not lost.
    links = _pdf_links(reader)
    return text + ("\n\nLinks in document:\n" + "\n".join(links) if links else "")


def _pdf_links(reader: PdfReader) -> list[str]:
    links = []
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            try:
                uri = annot.get_object().get("/A", {}).get("/URI")
            except Exception:
                continue
            if uri and str(uri) not in links:
                links.append(str(uri))
    return links


# ---------- normalisation ----------

def normalize_month(value: str | None) -> str | None:
    """'2021-3', 'Mar 2021', '03/2021', '2021' -> 'YYYY-MM' (year-only -> 'YYYY'). Unparseable -> None."""
    if not value:
        return None
    v = value.strip().lower()
    if v in {"present", "current", "till date", "now", "ongoing"}:
        return None
    if m := re.fullmatch(r"((?:19|20)\d{2})[-/.](\d{1,2})(?:[-/.]\d{1,2})?", v):
        year, month = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{1,2})[-/.]((?:19|20)\d{2})", v):
        year, month = int(m.group(2)), int(m.group(1))
    elif m := re.fullmatch(r"([a-z]{3})[a-z]*\.?[\s,'-]*((?:19|20)\d{2})", v):
        if m.group(1) not in _MONTHS:
            return None
        year, month = int(m.group(2)), _MONTHS[m.group(1)]
    elif m := re.fullmatch(r"((?:19|20)\d{2})", v):
        return m.group(1)
    else:
        return None
    return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else None


_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def normalize_linkedin(url: str | None) -> str | None:
    if not url:
        return None
    m = _LINKEDIN_RE.search(url)
    if not m:
        return None
    slug = m.group(0).rstrip("/").split("/in/")[-1]
    return f"https://www.linkedin.com/in/{slug}"


def _clean(resume: ParsedResume, text: str) -> ParsedResume:
    resume.linkedin_url = normalize_linkedin(resume.linkedin_url) or normalize_linkedin(text)
    if not resume.email and (m := _EMAIL_RE.search(text)):
        resume.email = m.group(0)
    for job in resume.jobs:
        job.employer = job.employer.strip()
        job.title = job.title.strip()
        job.start = normalize_month(job.start)
        if job.is_current:
            job.end = None
        else:
            job.end = normalize_month(job.end)
        if job.claimed_team_size is not None and job.claimed_team_size <= 0:
            job.claimed_team_size = None
        if job.employer_domain:
            job.employer_domain = re.sub(r"^(https?://)?(www\.)?", "", job.employer_domain.strip().lower()).split("/")[0]
    resume.jobs = [j for j in resume.jobs if j.employer]
    return resume


# ---------- public API ----------

def parse_resume(source: str | Path | bytes, filename: str | None = None) -> ParsedResume:
    text = extract_text(source, filename).strip()
    if len(text) < 30:
        raise ResumeParseError("Resume text is empty or too short to parse.")
    resume = complete_json(PROMPT.format(text=text[:MAX_CHARS]), ParsedResume)
    if not resume.full_name.strip():
        raise ResumeParseError("Could not find the candidate's name in the resume.")
    return _clean(resume, text)


if __name__ == "__main__":
    import sys

    print(parse_resume(sys.argv[1]).model_dump_json(indent=2))
