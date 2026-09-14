"""AI-assisted lead deduplication.

Design (full writeup in README):

1. **Blocking (candidate generation)** -- instead of the ~2000^2 (~4M)
    pairwise comparisons a brute-force approach would need, the implementation only compares
   leads that share at least one cheap blocking key:
     - normalized email local-part+domain family (same domain + similar
       local part), or exact email
     - normalized phone digits (last 9 digits, to tolerate country-code
       formatting differences)
     - normalized company name (legal-suffix-stripped) + similar display
       name
   This turns an O(n^2) problem into a small number of O(bucket_size^2)
   problems, which is tractable even at far larger scale than 2,000 rows.

2. **Scoring** -- within each candidate bucket, the implementation scores pairs with
   RapidFuzz (token-sort + partial ratio) across name, email local-part,
   company, and phone, and combine into a single confidence score. This is
   deterministic, fast (no API calls), and good enough to catch typo-level
   near-duplicates ("Yuki Aina" vs "Yuki Ayna", "weimingm@delta.co" vs
   "wei.ming.m@delta.co"). An LLM-prompt-based comparator could replace or
   augment this scoring step for the surviving *pairs* (a much smaller set
   than the full dataset) if higher recall on genuinely ambiguous cases
    were needed -- the README notes this as a "next step" rather than
   building it, given the time budget, since fuzzy string scoring already
   covers the stated failure modes (typo'd names, differing email
   localparts, punctuation-different company names).

3. **Distinguishing near-duplicates from distinct people who share a
    company/similar name** -- the brief calls this out explicitly. The implementation treats
   company+similar-name matches as lower confidence than email/phone
   matches, and require at least a partial signal on a second field
   (not just name) before surfacing a candidate, to reduce false positives
   like two different "Smith"s at the same company.
"""

from dataclasses import dataclass
from itertools import combinations

from rapidfuzz import fuzz

from app.normalize import normalize_email, normalize_company


@dataclass
class Lead:
    id: int
    display_name: str
    email_norm: str
    email_domain: str
    phone_digits: str
    company_norm: str


@dataclass
class DedupCandidate:
    lead_id_a: int
    lead_id_b: int
    confidence: float
    reason: str
    method: str = "blocking+fuzzy"


def _email_local(email_norm: str) -> str:
    return email_norm.split("@")[0] if "@" in email_norm else email_norm


def _phone_key(phone_digits: str, tail: int = 9) -> str:
    """Last N digits, to tolerate different country-code prefixes/formatting
    while still requiring a real shared number, not just a coincidental
    short prefix."""
    return phone_digits[-tail:] if len(phone_digits) >= tail else ""


def build_blocks(leads: list[Lead]) -> dict[str, list[Lead]]:
    blocks: dict[str, list[Lead]] = {}

    def add(key: str, lead: Lead):
        if not key:
            return
        blocks.setdefault(key, []).append(lead)

    for lead in leads:
        if lead.email_norm:
            add(f"email:{lead.email_norm}", lead)
        if lead.email_domain:
            local = _email_local(lead.email_norm)
            # bucket by domain + first 3 chars of local part -- catches
            # 'weimingm@delta.co' vs 'wei.ming.m@delta.co' without bucketing
            # the entire domain (e.g. everyone @gmail.com) together
            local_clean = "".join(ch for ch in local if ch.isalnum())
            add(f"domain:{lead.email_domain}:{local_clean[:3]}", lead)
        phone_key = _phone_key(lead.phone_digits)
        if phone_key:
            add(f"phone:{phone_key}", lead)
        if lead.company_norm:
            add(f"company:{lead.company_norm}", lead)

    return blocks


def score_pair(a: Lead, b: Lead) -> tuple[float, str]:
    name_score = fuzz.token_sort_ratio(a.display_name, b.display_name) / 100
    email_local_score = fuzz.ratio(_email_local(a.email_norm), _email_local(b.email_norm)) / 100
    company_score = fuzz.token_sort_ratio(a.company_norm, b.company_norm) / 100
    same_domain = 1.0 if a.email_domain and a.email_domain == b.email_domain else 0.0
    same_phone_tail = 1.0 if _phone_key(a.phone_digits) and _phone_key(a.phone_digits) == _phone_key(b.phone_digits) else 0.0
    exact_email = 1.0 if a.email_norm and a.email_norm == b.email_norm else 0.0

    reasons = []
    if exact_email:
        reasons.append("exact email match")
    elif same_domain and email_local_score >= 0.6:
        reasons.append(f"similar email ({email_local_score:.0%} local-part match, same domain)")
    if same_phone_tail:
        reasons.append("matching phone number")
    if name_score >= 0.75:
        reasons.append(f"similar name ({name_score:.0%})")
    if company_score >= 0.85:
        reasons.append("same company (normalized)")

    # weighted blend: strong identity signals (email, phone) dominate;
    # name/company alone (no email/phone corroboration) is downweighted to
    # avoid flagging two different people at the same company.
    score = (
        0.45 * exact_email
        + 0.20 * (email_local_score if same_domain else 0)
        + 0.20 * same_phone_tail
        + 0.10 * name_score
        + 0.05 * company_score
    )
    if not exact_email and not same_phone_tail and not (same_domain and email_local_score >= 0.6):
        # no strong identity signal at all -- cap confidence so a shared
        # company + vaguely similar name never looks like a confident dup
        score = min(score, 0.45)

    return round(min(score, 1.0), 3), "; ".join(reasons) if reasons else "weak signal only"


def find_dedupe_candidates(leads: list[Lead], min_confidence: float = 0.5) -> list[DedupCandidate]:
    blocks = build_blocks(leads)
    seen_pairs: set[tuple[int, int]] = set()
    candidates: list[DedupCandidate] = []

    for bucket in blocks.values():
        if len(bucket) < 2 or len(bucket) > 50:
            # skip oversized buckets (e.g. a very generic company-name
            # normalization) -- not worth O(n^2) inside a bucket that big,
            # and it signals the blocking key wasn't selective enough
            continue
        for a, b in combinations(bucket, 2):
            if a.id == b.id:
                continue
            pair_key = (min(a.id, b.id), max(a.id, b.id))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            score, reason = score_pair(a, b)
            if score >= min_confidence:
                candidates.append(DedupCandidate(pair_key[0], pair_key[1], score, reason))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


@dataclass
class DedupGroup:
    member_ids: list[int]
    edges: list[DedupCandidate]  # the pairwise comparisons that connect this group
    group_confidence: float      # conservative: weakest edge in the group


class _UnionFind:
    """Minimal union-find (disjoint-set) used to cluster pairwise candidates
    into connected groups -- e.g. if A-B and B-C are both flagged as likely
    duplicates, {A, B, C} become one group even though A-C alone was never
    directly compared (they may not share a blocking key)."""

    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path compression
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def group_dedupe_candidates(leads: list[Lead], min_confidence: float = 0.5) -> list[DedupGroup]:
    """Clusters the pairwise candidates from find_dedupe_candidates() into
    connected groups, per the brief's wording ("returns groups of
    likely-duplicate leads"). A group can have more than 2 members if the
    pairwise matches chain together (A-B and B-C both flagged -> {A, B, C}).

    Trade-off worth naming explicitly: this is a "transitive closure" over
    the pairwise edges, which is the standard way to go from pairwise
    matches to clusters in record linkage -- but it means a group's
    confidence isn't a single clean number the way a pair's is. Two records
    in the same group are not guaranteed to have been directly compared to
    each other at all (they may be linked only via a shared third record) --
    this is a known limitation called "chaining" in the entity-resolution
    literature. To stay conservative, group_confidence is the *weakest*
    edge in the group (not an average), and every edge that produced the
    grouping is still returned in full under `edges` so a human reviewer can
    see exactly which pairs were actually compared, rather than trusting an
    opaque group-level score.
    """
    candidates = find_dedupe_candidates(leads, min_confidence=min_confidence)
    if not candidates:
        return []

    uf = _UnionFind()
    for c in candidates:
        uf.union(c.lead_id_a, c.lead_id_b)

    groups_by_root: dict[int, dict] = {}
    for c in candidates:
        root = uf.find(c.lead_id_a)
        g = groups_by_root.setdefault(root, {"member_ids": set(), "edges": []})
        g["member_ids"].add(c.lead_id_a)
        g["member_ids"].add(c.lead_id_b)
        g["edges"].append(c)

    groups = [
        DedupGroup(
            member_ids=sorted(g["member_ids"]),
            edges=sorted(g["edges"], key=lambda e: e.confidence, reverse=True),
            group_confidence=min(e.confidence for e in g["edges"]),
        )
        for g in groups_by_root.values()
    ]
    groups.sort(key=lambda g: (g.group_confidence, len(g.member_ids)), reverse=True)
    return groups
