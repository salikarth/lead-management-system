"""AI-assisted source extraction.

Design decision (see README for the full writeup): the implementation uses a **rules/regex
pass first**, and only fall back to an LLM call for notes the rules can't
confidently classify. Reasoning:

- The example notes in the brief ("Met him at the SFF booth, scanned our QR
  code", "Found us through organic google search then booked a demo") are
  built from a fairly small, recognizable vocabulary of channel signals
  (booth/QR/conference -> Event; "google search"/"organic" -> Organic
  Search; "linkedin" -> LinkedIn; "referred by"/"warm intro" -> Referral;
  "filled out the form"/"contact page" -> Website; "cold call"/"outbound"/
  sales rep language -> Manual/Sales). A regex pass over ~2,000 rows is
  free, instant, and fully deterministic/auditable -- important for a CRM
  field a sales team will filter and report on.
- Reserving the (slower, costlier, non-deterministic) LLM call for the
  genuinely ambiguous remainder keeps the system practical at scale and
  keeps the "why did it choose this channel" answer inspectable for the
  easy majority of rows.
- No paid API key was used for this submission (see README) -- the LLM
  path calls the Google Gemini API (has a standing free tier as of 2026,
  no card required -- get a key at aistudio.google.com/apikey), gated
  behind a GEMINI_API_KEY env var. Disabled by default (LLM_ENABLED=False)
  so the project runs standalone, deterministically, and without
  credentials out of the box.
"""

import json
import os
import re
import urllib.request
from dataclasses import dataclass, asdict

CHANNELS = [
    "Website",
    "Event",
    "LinkedIn",
    "Organic Search",
    "Referral",
    "Manual/Sales",
    "Other",
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
LLM_ENABLED = GEMINI_API_KEY is not None
GEMINI_MODEL = "gemini-flash-latest"


@dataclass
class SourceExtraction:
    channel: str
    detail: str
    method: str  # "rule" | "llm" | "fallback"


# Each rule: (compiled regex, channel, detail-extractor(match, text) -> str)
def _detail_after(keyword_re):
    def _extract(m, text):
        tail = text[m.end():].strip(" ,.-")
        return tail if tail else text.strip()
    return _extract


_RULES = [
    (re.compile(r"\bqr code\b", re.I), "Event", None),
    (re.compile(r"\b(booth|conference|expo|summit|festival|trade show|meetup)\b", re.I), "Event", None),
    (re.compile(r"\blinkedin\b", re.I), "LinkedIn", None),
    (re.compile(r"\bgoogl(e|ed)\b.*\b(ad|ads|advert)", re.I), "Other", None),
    (re.compile(r"\b(googled us|organic|google search|search engine|seo)\b", re.I), "Organic Search", None),
    (re.compile(r"\b(referred by|referrals?|warm intro|introduced by|intro from)\b", re.I), "Referral", None),
    (re.compile(r"\b(filled out|contact page|contact form|signed up|newsletter|pricing page|requested a demo|demo request|booked a demo|book-a-demo|product tour page|homepage)\b", re.I), "Website", None),
    (re.compile(r"\b(cold call|cold email|outbound|sales rep|prospected|reached out to them|we reached out|manual(ly)? added|inbound phone call|phone call from|walked into our office|general info@|info@ inbox|cold outreach)\b", re.I), "Manual/Sales", None),
]


def _split_sentences(text: str) -> list[str]:
    # simple sentence splitter -- good enough for short CRM notes, which
    # are typically 1-3 short sentences separated by '. '
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _rule_based(text: str) -> SourceExtraction | None:
    text = (text or "").strip()
    if not text:
        return None
    sentences = _split_sentences(text)
    for pattern, channel, _ in _RULES:
        for sentence in sentences:
            m = pattern.search(sentence)
            if m:
                # use only the matching sentence(s) as detail, not the whole
                # note -- trailing CRM-activity sentences ("Connected,
                # sending proposal.") aren't part of the *source*, so we
                # drop them rather than echoing the full note back
                detail = sentence.rstrip(".").strip()
                return SourceExtraction(channel=channel, detail=detail, method="rule")
    return None


def _build_prompt(text: str) -> str:
    return (
        "Classify this CRM lead note into a channel and a short detail.\n"
        f"Allowed channels (choose exactly one): {', '.join(CHANNELS)}\n"
        "Respond ONLY with JSON: {\"channel\": ..., \"detail\": ...}\n"
        "'detail' should be a short (<12 word) human-readable summary of the "
        "specific source (e.g. event name, referrer name, search term), or "
        "the note itself if nothing more specific is extractable.\n\n"
        f"Note: {text}"
    )


def _parse_llm_json(raw: str, fallback_text: str) -> SourceExtraction:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(raw)
    channel = parsed.get("channel", "Other")
    if channel not in CHANNELS:
        channel = "Other"
    return SourceExtraction(channel=channel, detail=parsed.get("detail", fallback_text), method="llm")


def _gemini_based(text: str) -> SourceExtraction:
    """Real call to the Google Gemini API (generateContent). Gemini has a
    standing free tier as of 2026 (no card required) -- see
    https://aistudio.google.com/apikey. Used only when GEMINI_API_KEY is
    set and the rules pass didn't match."""
    body = json.dumps({
        "contents": [{"parts": [{"text": _build_prompt(text)}]}],
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        raw = "".join(
            p.get("text", "")
            for p in data["candidates"][0]["content"]["parts"]
        )
        return _parse_llm_json(raw, text)
    except Exception:
        return SourceExtraction(channel="Other", detail=text, method="fallback")


def _llm_based(text: str) -> SourceExtraction:
    return _gemini_based(text)


def extract_source(notes: str, original_source: str = "") -> SourceExtraction:
    """Public entrypoint. Tries rules on `notes` first (the brief explicitly
    says not to trust `Original Source`), falls back to `original_source`
    if notes are empty, then to the LLM (if enabled), then to a generic
    'Other' bucket with the raw text preserved for manual review."""
    text = (notes or "").strip()
    result = _rule_based(text)
    if result:
        return result

    if original_source:
        result = _rule_based(original_source)
        if result:
            return result

    if LLM_ENABLED and text:
        return _llm_based(text)

    fallback_text = text or original_source or ""
    return SourceExtraction(channel="Other", detail=fallback_text or "(no source info)", method="fallback")


def extract_source_dict(notes: str, original_source: str = "") -> dict:
    return asdict(extract_source(notes, original_source))
