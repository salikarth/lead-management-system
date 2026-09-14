# Mini Lead Management System

A scoped-down lead tracking backend built for the WIZ.AI AI Engineer take-home assignment.
Loads a messy ~2,000-row synthetic HubSpot-style CSV export, exposes a REST API over it, and
includes two AI-assisted features: lead deduplication and free-text source extraction.

## How to run it

```bash
cd lead-system
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt

python -m app.load_seed          # loads data/leads_seed.csv into data/leads.db (SQLite)
uvicorn app.main:app --reload    # starts the API on http://localhost:8000
```

Interactive API docs (Swagger UI): `http://localhost:8000/docs`

Run the deterministic test suite:

```bash
python -m pytest tests/ -v
```

The full suite contains 85 tests: 83 deterministic tests (no network calls, no API key needed) covering
normalization edge cases, dedup scoring (including the "same company, different person"
false-positive case), source-extraction rules, the Gemini call path (mocked at the HTTP layer —
success, network failure, and malformed-response cases), and the API endpoints (filtering,
patch, ingest/merge, dedupe, dashboard, export, 404/400 paths) — plus 2 live integration tests
described below. Without `GEMINI_API_KEY`, the expected result is `83 passed, 2 skipped` because
the live Gemini tests are skipped.

There are also 2 **live** integration tests (`TestGeminiLiveIntegration` in
`tests/test_source_extraction.py`) that make real, non-mocked calls to the Gemini API. These are
skipped automatically unless `GEMINI_API_KEY` is set (`pytest.mark.skipif`), on purpose — they're
slower, cost API quota, and can fail for reasons that have nothing to do with the code (rate
limits, transient network issues), so they never block anyone (including a reviewer without a
key) from running the rest of the suite. Run them too by just setting the key first — no special
flag needed, `skipif` picks it up automatically:

```bash
export GEMINI_API_KEY="your-key-here"
python -m pytest tests/ -v
```

On Windows PowerShell, set the key in the same terminal before running pytest:

```powershell
$env:GEMINI_API_KEY = "your-key-here"
python -m pytest tests/ -v
```

The live tests require available Gemini quota. If the key is present but the account/project has
hit a rate or usage limit, the tests report `skipped` for those live cases after the application
falls back safely; this is an external service limitation, not a failure of the deterministic
application tests. The previously verified live run completed with `24 passed` for
`tests/test_source_extraction.py`. The documented live demo result below is retained as evidence
of a successful Gemini call.

This mirrors how the test pyramid is normally split in practice: many fast, deterministic,
mocked unit tests form the base and run on every change; a much smaller number of real
integration tests exist alongside them but run less often (manually, or on a schedule — not
gating every commit) precisely because they're non-deterministic and carry a real cost.

## Stack and design decisions

**Storage: SQLite.** No server to run, a single inspectable file, more than enough for 2,000
rows, and trivially reset (`load_seed.py --reset` recreates it from the CSV). Postgres would be
the obvious next step if this needed concurrent writers or to run alongside a real app.

**Framework: FastAPI.** Chosen for speed of development, automatic OpenAPI docs, and Pydantic
validation on `PATCH`/`POST` bodies "for free."

**Data model.** The `leads` table keeps the original CSV values (for display/audit) alongside a
handful of *derived, normalized* columns — `display_name`, `status_norm`, `email_norm`,
`email_domain`, `phone_digits`, `company_norm` — computed once at load/ingest time and used for
filtering, search, and dedup blocking. This avoids re-normalizing on every query and keeps the
raw data intact for a human to sanity-check. See `app/normalize.py`.

Specific messiness handled at load time:
- **Full Name vs First/Last**: `build_display_name()` prefers First+Last when both exist, falls
  back to Full Name, degrades gracefully to whichever single field is present. For the ~107 rows
  where only `Full Name` is populated (no `First Name`/`Last Name` at all), `split_full_name()`
  backfills `first_name`/`last_name` by splitting on the first space (first token → first name,
  remainder → last name) so those columns aren't left empty — `full_name` itself is kept as-is
  regardless.
- **Status casing/whitespace** (`New` / `new` / `NEW` / `" New"`): `normalize_status()` trims and
  title-cases into a canonical value used for filtering; the raw value is kept in `status`.
- **Country casing** (`China` / `china`, and 26 other countries the same way): the brief only
  calls this out for `Lead Status`, but the same casing inconsistency turns out to exist in
  `Country/Region` too. `normalize_country()` handles it the same way, via a `country_norm`
  column used for filtering (raw `country` kept as-is for display/audit).
- **Mixed date formats**: `normalize_date()` tries a small list of known formats
  (`YYYY-MM-DD`, `M/D/YYYY`, ISO with `Z`) and returns `''` rather than raising if none match —
  a bad date shouldn't block a row from loading.
- **Phone number formatting**: `normalize_phone_digits()` strips everything but digits for the
  full stored `phone_digits` column (country code digits are preserved, only formatting
  characters like `+`/spaces/parens are removed — no information is lost). Separately, the dedup
  *blocking key* (`app/dedup.py`) intentionally uses only the **last 9 digits** of that value, to
  tolerate the same person being entered once with a country code and once without. This is a
  deliberate precision/recall trade-off: it risks grouping two unrelated people who happen to
  share a local number under different country codes into the same *candidate block*, but the
  subsequent fuzzy-scoring step (name/email/company) is what actually decides confidence, so a
  shared phone tail alone doesn't produce a false "duplicate" verdict on its own.
- **Company name legal-suffix noise** (`Inc.` / `Inc` / `Co` / `& Co` / `Group` / ...):
  `normalize_company()` strips these and punctuation so `"Chen Digital Inc."` and
  `"Chen Digital, Co"` normalize to the same key for dedup blocking.
- **Untrustworthy `Original Source`**: mostly ignored in favor of parsing the free-text `Notes`
  field (see source extraction below); `Original Source` is only used as a fallback when `Notes`
  is empty or unclassifiable.

### 1. Lead store + API

All five required endpoints are implemented in `app/main.py`:
`GET /leads` (filter by `status`, `owner`, `country`, free-text `q` across name/company/email),
`GET /leads/:id`, `PATCH /leads/:id` (status/owner/notes — editing notes re-runs source
extraction so the derived channel stays in sync), `GET /leads/export` (CSV of the current
filtered view), `POST /leads/ingest` (see below). Plus `POST /leads/extract-source` (standalone
access to the source-classification logic — see part 3) and the bonus dashboard endpoints.

### `POST /leads/ingest` — create-or-merge

Matches an incoming website-form submission (shape: `data/website_form_submissions.json`) against
existing leads by **exact normalized email, then exact phone digits** — deliberately *not* fuzzy
matching here, since an automated ingest endpoint needs a decisive yes/no rather than a
confidence score. (Fuzzy near-duplicates from form resubmissions still get caught later by
`/leads/dedupe-candidates`, which a human reviews before merging.) On a match, the existing
lead's notes are appended to (not overwritten) and any previously-blank fields (phone, company,
country) are backfilled; on no match, a new lead is created with status `New`.

`form_id`, `page_url`, and `submitted_at` — fields present in the JSON payload with no equivalent
column in the original CRM export — are stored in dedicated `web_form_id` / `web_page_url` /
`web_submitted_at` columns rather than dropped or buried inside `notes`, since they're genuinely
useful marketing-attribution data (which page a lead was on when they converted). Unlike
phone/company/country above, these three are **always overwritten** on a repeat submission rather
than backfilled-only-if-blank: they represent the *most recent* touchpoint, so a second form
submission from the same person should update them, not preserve whatever was captured on the
first visit.

### 2. AI-assisted lead deduplication (`app/dedup.py`)

**The core problem**: ~2,000 rows means ~2,000² ≈ 4,000,000 possible pairs. Comparing all of
them (with fuzzy matching or an LLM call) isn't practical, so the approach is
**blocking → scoring**, not brute force:

1. **Blocking (candidate generation).** Each lead is bucketed under a handful of cheap keys:
   exact normalized email, `domain + first-3-chars-of-localpart` (catches
   `weimingm@delta.co` vs `wei.ming.m@delta.co` without bucketing an entire domain like
   `gmail.com` together), last-9-digits of phone (tolerates different country-code formatting on
   the same real number), and normalized company name. Only leads sharing a bucket are ever
   compared — this is the "narrow the candidate set first" the brief asks for. Buckets larger
   than 50 are skipped entirely (a bucket that big signals a non-selective key, e.g. a very
   generic company name, and isn't worth the O(n²) inside it). On the real 2,049-row dataset this
   runs in **~35ms** and finds candidate pairs from a search space of a few thousand comparisons,
   not 4 million.

2. **Scoring.** Within each bucket, pairs are scored with RapidFuzz (token-sort ratio on name and
   company, plain ratio on email local-part) combined into a weighted confidence score, with
   **email and phone signals weighted far more heavily than name/company alone**. This is the
   direct answer to the brief's callout about *distinct* people who share a company and a
   similar-sounding name: without a corroborating email or phone match, the score is explicitly
   capped at 0.45 so "two different Smiths at Acme" never looks like a confident duplicate
   (see `test_same_company_similar_name_different_person_scores_low`). Every candidate ships with
   a human-readable `reason` string (e.g. `"similar email (97% local-part match, same domain);
   matching phone number; similar name (100%); same company (normalized)"`) so a human reviewer
   can see why it was flagged, not just a bare score.

3. **Why fuzzy matching over an LLM call for scoring**: it's deterministic, free, and instant,
   and it directly targets the failure modes named in the brief (typo'd names, differing email
   localparts, differently-punctuated company suffixes). An LLM prompt-based comparator is a
   natural *next step* to add on top for the harder residual cases (e.g. genuinely different
   spellings of the same name across scripts, or judgment calls the reason string doesn't
   resolve) — because blocking has already cut the candidate set down to a few hundred pairs by
   that point, an LLM pass there is actually affordable, unlike at the full 2,000-row scale.
   I didn't build that layer given the time budget, since RapidFuzz already surfaces the exact
   cases the brief describes with a fully working, tested implementation.

Exposed via `POST /leads/dedupe-candidates?min_confidence=0.5`, returning ranked **groups** of
likely duplicates (no auto-merge, as specified) — matching the brief's own wording ("returns
groups of likely-duplicate leads"). Concretely:

4. **From pairs to groups**: `score_pair()` only ever compares two leads at a time, but a real
   duplicate can appear more than twice (three or more re-entries of the same person). Pairwise
   matches are clustered into groups via union-find/connected-components over the pairs that
   passed `min_confidence` — if A-B and B-C are both flagged, `{A, B, C}` become one group even
   though A and C may never have shared a blocking key and so were never directly compared. This
   is the standard way to go from pairwise matches to clusters in record linkage, but it's worth
   naming the trade-off explicitly: it can "chain" together records on the strength of a shared
   third record rather than direct similarity (known as the *chaining problem* in entity
   resolution). To stay conservative, a group's `group_confidence` is the **weakest** edge in the
   group, not an average, and every pairwise comparison that produced the grouping is still
   returned under `supporting_pairs` — so a reviewer can see exactly which pairs were *actually*
   compared rather than trusting an opaque group-level score. Groups sharing a display name alone
   (no corroborating email/phone/company signal) are never merged together, since that signal
   alone is capped below the default threshold (see point 2 above and
   `test_grouping_keeps_unrelated_same_name_people_in_separate_groups`).

### 3. AI-assisted source extraction (`app/source_extraction.py`)

**Approach: rules/regex first, LLM fallback for the unclassifiable remainder**, not an LLM call
per row. Reasoning:

- The example notes in the brief ("Met him at the SFF booth, scanned our QR code", "Found us
  through organic google search then booked a demo") are built from a fairly small, recognizable
  vocabulary of channel signals. A regex pass classifies these instantly, deterministically, and
  auditably — which matters for a CRM field a sales team will filter and report on. Running this
  against the actual seed data classified roughly 70% of the ~2,000 rows with zero ambiguity
  (see `GET /dashboard` output below).
- One nuance the rules deliberately get right rather than naively pattern-matching on "google":
  `"Booked a demo... after clicking a google ad"` is a **paid** channel, not Organic Search, even
  though it mentions Google — the paid-ad rule is checked before the organic-search rule
  specifically to avoid that false positive (see
  `test_paid_google_ad_is_not_misclassified_as_organic`).
- The remaining ambiguous notes fall through to an LLM call — but **disabled by default** (no
  API key is bundled), so the project runs standalone and deterministically without credentials.
  See "LLM usage" below for how to enable it, including a free-tier option.
- `Original Source` is used only as a fallback when `Notes` is empty, per the brief's warning
  that it's often blank or too generic (e.g. `"Offline Sources"` for an event lead).

Exposed both as an internal function (called automatically at load time and whenever `PATCH
/leads/:id` updates `notes`) and as a standalone `POST /leads/extract-source` endpoint for
testing arbitrary text directly. The rule path returns the *matching sentence* as `detail`
(not the whole note) so trailing CRM-activity commentary unrelated to the source itself
(e.g. `"Connected, sending proposal."`) is dropped — e.g. `"Referred by Michael Zhang, warm
intro. Connected, sending proposal."` yields `detail: "Referred by Michael Zhang, warm intro"`.

**Known limitation**: the rule-based `detail` is a cleaned sentence, not a fully extracted
entity the way the brief's example (`"Singapore FinTech Festival 2026 — Booth QR Code"`)
implies — it doesn't pull out just the event name or just the referrer's name. Getting from
"the matching sentence" to "just the named entity" reliably across arbitrary phrasing is
exactly the kind of judgment call an LLM handles well and regex doesn't generalize to; the LLM
path is prompted to return that tighter, entity-level detail, which is the main reason I'd
prioritize enabling it on a larger sample if given more time, rather than only using it for the
unclassifiable "Other" bucket.

Current classification breakdown on the real dataset (`GET /dashboard`):

| Channel | Count |
|---|---|
| Website | 388 |
| Event | 374 |
| Manual/Sales | 339 |
| Organic Search | 319 |
| Referral | 233 |
| Other | 210 |
| LinkedIn | 186 |

The remaining ~210 "Other" rows are genuinely ambiguous free text where a human (or the LLM
fallback) would need to make a judgment call — they aren't silently miscategorized.

### 4. Dashboard (bonus)

`GET /dashboard` returns the data used by the initial visual dashboard: lead counts by status and
source channel (table above), top-10 breakdowns by country and owner, and a month-by-month
created-leads trend. `GET /dashboard/view` serves a rendered version on top of this JSON (bar
charts + a trend line via Chart.js, loaded from a CDN with an automatic jsDelivr fallback — no
build step or extra dependencies).

The country chart supports interactive drill-down. Clicking a country calls
`GET /dashboard/drilldown?country=...` and shows the next useful breakdown. If city data exists,
the response returns ranked cities; clicking a city then calls
`GET /dashboard/drilldown?country=...&city=...` and shows lead status for that city. The supplied
dataset has no populated city values, so the current fallback goes directly from country to lead
status instead of displaying an unhelpful `Unknown` bar. The chart labels its active Y-axis as
`Country`, `City`, or `Status` accordingly, and provides a back button for navigation.

Open `http://localhost:8000/dashboard/view` after starting the server. The visual page fetches
`GET /dashboard` on load and the drill-down endpoint only when a country or city is clicked; refresh
the page to see updated data after ingesting or editing leads. Country/owner summaries are capped
at the top 10 to keep the chart readable; use `GET /leads?country=...` or `?owner=...` for exact
lead-level filtering.

## LLM usage / cost

No paid API key was used for this submission — the deterministic rules pass covers the stated
examples and the majority of the real dataset, so I didn't want to spend on an LLM call I
couldn't demonstrate necessity for within the time budget.

`app/source_extraction.py` calls the **Google Gemini API** (`gemini-flash-latest`), gated behind
a `GEMINI_API_KEY` env var. Gemini has a standing free tier as of 2026 (no card required) — get a
key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

To try it against the real ~210-row "Other" bucket without spending anything:

```bash
export GEMINI_API_KEY="your-key-here"
uvicorn app.main:app --reload
```

The call is a real, working HTTP request (`_gemini_based()` in `app/source_extraction.py`) — not
mocked — and fails closed to `method: "fallback"` rather than crashing if the call errors out (bad
key, rate limit, network issue, etc.), so the rest of the system keeps working even if the LLM
call fails.

### Live Gemini demo result

The Gemini fallback was also exercised successfully through `POST /leads/extract-source` with an
ambiguous note that does not match the deterministic rules:

```json
{
  "notes": "Someone from their team stumbled onto us while browsing around during their coffee break at a big tech gathering downtown"
}
```

The live API returned HTTP `200` with:

```json
{
  "channel": "Event",
  "detail": "Tech gathering downtown",
  "method": "llm"
}
```

This confirms that the Gemini path can classify an ambiguous note and return the expected
structured response. The live demo consumed the available API quota, so subsequent live calls
may be unavailable until the quota resets; the mocked tests remain runnable without a key.

Evidence screenshots:

- [Successful Gemini extraction response](AI_Feature_Evidence.jpeg)
- [Gemini-enabled test run](AI_Feature_Evidence2.jpeg)

## What I'd do next with more time

- **LLM-assisted dedup for the post-blocking residual**: once blocking narrows ~4M possible pairs
  down to a few hundred candidates, an LLM comparator (or even just a second-pass prompt over the
  borderline 0.5–0.7 confidence band) could resolve genuinely ambiguous cases the fuzzy scorer
  under- or over-calls.
- **Embeddings-based blocking** as a complement to the exact-key blocking here — would catch
  cases with no shared email/phone/company key at all (e.g. a company renamed between two
  submissions), which the current blocking strategy would miss entirely.
- **Auto-merge workflow** with an audit trail, once dedup confidence is trusted enough — currently
  intentionally out of scope per the brief.
- **Pagination cursor** instead of offset for `/leads` at real scale, and moving to Postgres if
  this needed concurrent writers.
- Wire the LLM source-extraction fallback into a small eval set to measure agreement with the
  rules pass, before trusting it on the "Other" bucket unsupervised.

## Explicitly out of scope

Per the brief: authentication/roles, GA/analytics integration, webhook infra beyond
`/leads/ingest`, full audit-log UI, HubSpot migration tooling, deployment/scaling/monitoring.

## Project structure

```
lead-system/
├── app/
│   ├── main.py                # FastAPI app, all endpoints
│   ├── db.py                  # SQLite connection + schema
│   ├── normalize.py           # data-cleaning helpers
│   ├── load_seed.py           # CSV -> SQLite loader
│   ├── source_extraction.py   # rules + optional Gemini channel classifier
│   ├── dedup.py                # blocking + fuzzy-scoring + grouping dedup
│   └── static/
│       └── dashboard.html      # rendered chart view for GET /dashboard/view
├── tests/                      # pytest, 85 tests (83 always-run + 2 live/opt-in)
├── data/
│   ├── leads_seed.csv
│   ├── website_form_submissions.json
│   └── leads.db                # generated by load_seed.py
├── AI_Feature_Evidence.jpeg    # successful live Gemini extraction response
├── AI_Feature_Evidence2.jpeg   # sanitized Gemini-enabled test run evidence
├── debug_gemini.py             # standalone troubleshooting script for the Gemini API call
├── pytest.ini
└── requirements.txt
```
