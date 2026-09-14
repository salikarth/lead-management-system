"""Normalization helpers.

The seed data is messy on purpose (see README "About the dataset"). Rather
than mutating the original columns, the module computes derived/normalized versions
used for search, filtering, and dedup blocking, and keep the raw values
around for display and auditability.
"""

import re
from datetime import datetime

COMPANY_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|co|co\.|corp|corp\.|company|"
    r"group|holdings|trading|studio|& co|and co)\b\.?",
    re.IGNORECASE,
)


def clean_str(value) -> str:
    """Trim whitespace and collapse it; return '' for null-ish values."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("", "nan", "none", "null"):
        return ""
    return re.sub(r"\s+", " ", s)


def normalize_status(value) -> str:
    """Collapse casing/whitespace variants ('New', 'new', 'NEW', ' New') to
    a canonical Title Case status."""
    s = clean_str(value)
    if not s:
        return "Unknown"
    return s.strip().title()


def build_display_name(first, last, full) -> str:
    """Some rows only have Full Name, others only First/Last. Prefer
    First+Last when both are present (more structured), fall back to
    Full Name, and degrade gracefully otherwise."""
    first, last, full = clean_str(first), clean_str(last), clean_str(full)
    if first and last:
        return f"{first} {last}"
    if full:
        return full
    if first or last:
        return first or last
    return "(no name)"


def split_full_name(full: str):
    """Best-effort split of a Full Name into first/last for rows that only
    provide Full Name, so downstream first/last-based logic still has
    something to work with."""
    full = clean_str(full)
    if not full:
        return "", ""
    parts = full.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def normalize_country(value) -> str:
    """Collapse casing variants ('China' / 'china', 'Australia' /
    'australia', etc.) the same way normalize_status() does for Lead
    Status -- the seed data turns out to have this same messiness in
    Country/Region for 27 countries, it's just not called out explicitly
    in the brief's 'About the dataset' section."""
    s = clean_str(value)
    if not s:
        return ""
    return s.title()


def normalize_email(value) -> str:
    return clean_str(value).lower()


def email_domain(value) -> str:
    e = normalize_email(value)
    return e.split("@")[-1] if "@" in e else ""


def normalize_phone_digits(value) -> str:
    """Strip everything but digits. The function deliberately does NOT try to validate
    or reformat into E.164 -- for blocking purposes, matching digit
    sequences (allowing for a shared national number with different
    country-code formatting) is good enough, and doing it on the last N
    digits catches '+1 415 555 0100' vs '415-555-0100' vs '(415) 555-0100'.
    """
    s = clean_str(value)
    return re.sub(r"\D", "", s)


def normalize_company(value) -> str:
    """Lowercase, strip legal suffixes and punctuation, collapse whitespace.
    e.g. 'Chen Digital Inc.' and 'Chen Digital, Inc' and 'Chen Digital Co'
    should normalize close enough to block together."""
    s = clean_str(value).lower()
    s = s.replace("&", "and")
    s = re.sub(r"[.,]", "", s)
    s = COMPANY_SUFFIXES.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%y",
]


def normalize_date(value) -> str:
    """Parse the handful of date formats seen in the seed data into a
    canonical ISO 'YYYY-MM-DD' string. Returns '' if unparseable, rather
    than raising, since a bad date shouldn't block a whole row from loading."""
    s = clean_str(value)
    if not s:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # last resort: try fromisoformat (handles some extra ISO variants)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""
