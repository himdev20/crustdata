"""Small helpers shared by the check modules: name normalisation, fuzzy scores, month maths."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from rapidfuzz import fuzz

# Legal / filler words that differ between a resume and a company registry.
_LEGAL = {
    "pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "corp", "corporation", "co", "company",
    "plc", "gmbh", "llc", "opc", "the", "india", "in",
}
# Descriptive words people drop ("Infosys Technologies Ltd" -> "Infosys").
_GENERIC = {
    "technologies", "technology", "tech", "solutions", "services", "software", "systems", "labs", "consulting",
    "consultancy", "infotech", "global", "group", "enterprises", "industries", "digital", "ventures",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and ")).split()


def normalize_company(name: str | None) -> str:
    return " ".join(t for t in _tokens(name or "") if t not in _LEGAL)


def company_core(name: str | None) -> str:
    return " ".join(t for t in _tokens(name or "") if t not in _LEGAL | _GENERIC)


def company_similarity(a: str | None, b: str | None) -> float:
    """0-100. Strict on extra words ("Zoho" vs "Zoho Belgium" ~50), lenient on legal suffixes."""
    na, nb = normalize_company(a), normalize_company(b)
    if not na or not nb:
        return 0.0
    score = fuzz.ratio(na, nb)
    ca, cb = company_core(a), company_core(b)
    if ca and cb:
        score = max(score, fuzz.ratio(ca, cb) - 5)
    return float(score)


_TITLE_ABBREV = {
    "sde": "software development engineer", "swe": "software engineer", "sr": "senior", "jr": "junior",
    "mgr": "manager", "engg": "engineering", "eng": "engineer", "dev": "developer", "assoc": "associate",
    "asst": "assistant", "vp": "vice president", "avp": "assistant vice president", "ii": "2", "iii": "3", "i": "1",
}


def normalize_title(title: str | None) -> str:
    return " ".join(_TITLE_ABBREV.get(t, t) for t in _tokens(title or ""))


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return float(max(fuzz.token_sort_ratio(na, nb), fuzz.token_set_ratio(na, nb) - 10))


def person_similarity(a: str | None, b: str | None) -> float:
    return float(fuzz.token_sort_ratio(" ".join(_tokens(a or "")), " ".join(_tokens(b or ""))))


# ---------- months ----------

def to_month(value: str | None) -> tuple[int, int] | None:
    """'2021-03', '2021-03-01T00:00:00', '2021' -> (2021, 3) / (2021, None->1)."""
    if not value:
        return None
    m = re.match(r"((?:19|20)\d{2})(?:-(\d{1,2}))?", str(value))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 1)


def month_precision(value: str | None) -> str:
    return "month" if value and re.match(r"\d{4}-\d{1,2}", str(value)) else "year"


def months_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Signed b - a in months."""
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def month_index(m: tuple[int, int]) -> int:
    return m[0] * 12 + m[1] - 1


def current_month() -> tuple[int, int]:
    d = datetime.now(timezone.utc)
    return d.year, d.month


def fmt_month(m: tuple[int, int] | None) -> str:
    return f"{m[0]:04d}-{m[1]:02d}" if m else "?"
