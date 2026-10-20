"""
Text normalisation for Naukri's human-formatted fields.

Naukri renders experience as "3-6 Yrs", salary as "12-18 Lacs PA" / "₹ 8,00,000
- 12,00,000 PA" / "Not disclosed", and freshness as "Just now" / "3 days ago" /
"30+ days ago". Filters need numbers, so all parsing lives here — pure functions,
fully unit-testable, no Playwright dependency.

Design decision: parsing is intentionally forgiving and returns `None` on
failure. A `None` never rejects a job (see filters.py) because a parse miss is
our bug, not a reason to skip a good opportunity.
"""

from __future__ import annotations

import re

_NUM = r"(\d+(?:\.\d+)?)"

# Markers proving a figure is compensation. Checked as substrings against the
# lowercased salary text; without one the number is contextless (bonus,
# shares, equity %, headcount) and must fail open, never invent lakhs.
_PAY_SIGNALS = (
    "lac", "lakh", "lpa", "salary", "ctc", "compensation",
    "per annum", "p.a", "annual", "pay", "inr", "₹", "rupee", "rupees",
    "wage", "stipend", "$", "usd", "dollar",
)

# Rough USD->INR for "$ 80k"-style listings. Conservative on purpose; the
# salary gate is a floor, so a slightly stale rate only errs toward keeping.
USD_TO_INR = 83.0


def parse_experience(text: str) -> tuple[float | None, float | None]:
    """'3-6 Yrs' -> (3, 6); '5+ years' -> (5, None); 'Fresher' -> (0, 0)."""
    if not text:
        return None, None
    cleaned = text.lower().replace("yrs", "").replace("years", "").replace("year", "")
    if "fresher" in text.lower():
        return 0.0, 0.0
    span = re.search(rf"{_NUM}\s*[-–to]+\s*{_NUM}", cleaned)
    if span:
        # Identical high bands ("24-24", "12-12") are headcount/applicant
        # artifacts, not tenure — nobody writes a genuine X-X band at 10+.
        # Fail open so a junior role isn't skipped on a stray number.
        if float(span.group(1)) == float(span.group(2)) and float(span.group(1)) >= 10:
            return None, None
        return float(span.group(1)), float(span.group(2))
    plus = re.search(rf"{_NUM}\s*\+", cleaned)
    if plus:
        return float(plus.group(1)), None
    single = re.search(_NUM, cleaned)
    if single:
        # Bare singles ("25 applicants", "3 openings") are headcount, not
        # tenure: accept only when the field names a tenure unit. Ranges
        # ("3-8") and plus-bands ("5+") are self-evidently tenure in an exp
        # field. Fail open (None) — unknown never rejects downstream.
        if not re.search(r"yrs?|years?|\bexp\b|fresher", text.lower()):
            return None, None
        value = float(single.group(1))
        return value, value
    return None, None


_EXP_CONTEXT_RE = re.compile(r"experien\w*|\bexp\b|\byrs\b|\byear\b", re.IGNORECASE)


def extract_jd_experience(text: str) -> tuple[float | None, float | None]:
    """Strongest experience requirement stated in a full JD (pure).

    Scans requirement-context lines ("experience", "exp", "yrs", "year") and
    returns the FIRST min-max band found. (None, None) when nothing found —
    caller keeps card numbers. JDs lead with the real requirement; taking the
    first avoids both over-applying to senior bands and skipping on junior
    side-mentions. Lines without experience context never qualify ("8-10
    openings" is headcount, not tenure).
    """
    if not text:
        return None, None
    for line in text.splitlines():
        line = line.strip()
        if not line or _EXP_CONTEXT_RE.search(line) is None:
            continue
        cleaned = line.lower().replace("yrs", "").replace("years", "").replace("year", "")
        span = re.search(rf"{_NUM}\s*[-–to]+\s*{_NUM}", cleaned)
        if span:
            lo, hi = float(span.group(1)), float(span.group(2))
            # Sanity: "Year of establishment: 2019" is not tenure. Absurd
            # bands belong to some other number class — skip the line.
            # Identical high bands ("24-24 openings") are headcount likewise.
            if lo <= 30 and hi <= 30 and not (lo == hi and lo >= 10):
                return lo, hi
            continue
        plus = re.search(rf"{_NUM}\s*\+", cleaned)
        if plus:
            if float(plus.group(1)) <= 30:
                return float(plus.group(1)), None
            continue
        single = re.search(_NUM, cleaned)
        if single:
            value = float(single.group(1))
            if value <= 30:
                return value, value
    return None, None


def parse_salary_lpa(text: str) -> tuple[float | None, float | None]:
    """
    Normalise to LPA (lakhs per annum).
    '12-18 Lacs PA'      -> (12, 18)
    '₹ 8,00,000 - 12,00,000 PA' -> (8, 12)
    '50,000 - 1,00,000 P.A. monthly' -> (6, 12)
    'Not disclosed'      -> (None, None)
    '₹500 per hour' / '500 bonus' / '6-10' -> (None, None): hourly rates and
    marker-less figures are never annual salaries — fail open (unknown never
    rejects downstream) instead of inventing lakhs.
    """
    if not text:
        return None, None
    lowered = text.lower()
    if "not disclosed" in lowered or "unpaid" in lowered:
        return None, None

    is_monthly = (
        "month" in lowered
        or "/month" in lowered
        or re.search(r"\bper\s+month\b|\bmonthly\b|\bp\.?\s*m\.?\b", lowered) is not None
    )
    # Hourly/daily/weekly/shift rates are never annual salaries: fail open
    # (None) instead of inventing lakhs — "₹500 per hour" once read as 500 LPA
    # and sailed through the salary gate.
    if re.search(
        r"\bper\s+(hour|hr|day|week|shift|task|assignment)\b"
        r"|/(hour|hr|day|week)\b"
        r"|\b(hourly|daily|weekly)\b"
        r"|\bshift\s+basis\b",
        lowered,
    ):
        return None, None
    # A bare number with no pay marker is contextless (bonus, shares, equity %,
    # headcount) — not a salary. Fail open instead of inventing lakhs.
    # ("rs" needs a word boundary: plain substring would fire on "years".)
    if not any(sig in lowered for sig in _PAY_SIGNALS) and re.search(
        r"\brs\.?\b", lowered
    ) is None:
        return None, None
    is_usd = "$" in lowered or "usd" in lowered or "dollar" in lowered
    # Strip currency symbols and Indian digit grouping.
    numeric = re.sub(r"[₹,]", "", lowered)
    values = [float(v) for v in re.findall(_NUM, numeric)]
    if not values:
        return None, None

    def to_lpa(value: float) -> float:
        if is_usd:
            value = value * USD_TO_INR
        if is_monthly:
            value = value * 12
        if value >= 100_000:  # absolute rupees
            return round(value / 100_000, 2)
        if value >= 1_000:  # e.g. "8,00,000" already divided, or thousands
            return round(value / 100_000, 2) if value > 10_000 else round(value / 1_000, 2)
        return round(value, 2)  # already in lakhs

    if "lac" in lowered or "lakh" in lowered or "lpa" in lowered:
        converted = [round(v * 12, 2) if is_monthly else round(v, 2) for v in values]
    else:
        converted = [to_lpa(v) for v in values]

    low = min(converted)
    high = max(converted)
    return low, (high if high != low else None)


def parse_posted_days(text: str) -> int | None:
    """'Just now'/'Today' -> 0, '3 days ago' -> 3, '30+ days ago' -> 30."""
    if not text:
        return None
    lowered = text.lower().strip()
    if any(token in lowered for token in ("just now", "few minutes", "today", "hour")):
        return 0
    if "yesterday" in lowered:
        return 1
    days = re.search(rf"{_NUM}\s*\+?\s*day", lowered)
    if days:
        return int(float(days.group(1)))
    weeks = re.search(rf"{_NUM}\s*\+?\s*week", lowered)
    if weeks:
        return int(float(weeks.group(1)) * 7)
    months = re.search(rf"{_NUM}\s*\+?\s*month", lowered)
    if months:
        return int(float(months.group(1)) * 30)
    return None


def parse_rating(text: str) -> float | None:
    if not text:
        return None
    match = re.search(_NUM, text)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 < value <= 5 else None


def normalise_whitespace(text: str) -> str:
    return " ".join((text or "").split())
