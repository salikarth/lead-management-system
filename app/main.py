"""Lead Management API.

Run: uvicorn app.main:app --reload
Docs: http://localhost:8000/docs
"""

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from app.db import db_cursor, get_connection, init_db
from app.normalize import (
    clean_str,
    normalize_status,
    build_display_name,
    normalize_email,
    email_domain,
    normalize_phone_digits,
    normalize_company,
    normalize_country,
)
from app.source_extraction import extract_source
from app.dedup import Lead as DedupLead, group_dedupe_candidates

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db(reset=False)
    yield


app = FastAPI(
    title="Mini Lead Management System",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- schemas --

class LeadUpdate(BaseModel):
    status: Optional[str] = None
    owner: Optional[str] = None
    notes: Optional[str] = None


class IngestPayload(BaseModel):
    form_id: Optional[str] = None
    form_name: Optional[str] = None
    page_url: Optional[str] = None
    submitted_at: Optional[str] = None
    name: Optional[str] = None
    email: str
    phone: Optional[str] = None
    company: Optional[str] = None
    country: Optional[str] = None
    message: Optional[str] = None


def _row_to_dict(row) -> dict:
    return dict(row)


# --------------------------------------------------------------- /leads ----

@app.get("/leads")
def list_leads(
    status: Optional[str] = None,
    owner: Optional[str] = None,
    country: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
):
    clauses, params = [], []
    if status:
        clauses.append("status_norm = ?")
        params.append(normalize_status(status))
    if owner:
        clauses.append("owner = ?")
        params.append(owner)
    if country:
        clauses.append("country_norm = ?")
        params.append(normalize_country(country))
    if q:
        clauses.append(
            "(display_name LIKE ? OR company_name LIKE ? OR email LIKE ?)"
        )
        like = f"%{q}%"
        params += [like, like, like]

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    total = conn.execute(f"SELECT COUNT(*) FROM leads {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM leads {where} ORDER BY id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    return {"total": total, "limit": limit, "offset": offset, "leads": [_row_to_dict(r) for r in rows]}


@app.get("/leads/export")
def export_leads(
    status: Optional[str] = None,
    owner: Optional[str] = None,
    country: Optional[str] = None,
    q: Optional[str] = None,
):
    result = list_leads(status=status, owner=owner, country=country, q=q, limit=100000, offset=0)
    leads = result["leads"]

    buf = io.StringIO()
    fieldnames = [
        "id", "record_id", "first_name", "last_name", "full_name", "job_title",
        "company_name", "email", "phone_number", "country", "country_norm", "city", "status",
        "lifecycle_stage", "original_source", "original_source_drilldown", "owner",
        "create_date", "last_modified_date", "notes", "lead_score", "display_name",
        "status_norm", "email_norm", "email_domain", "phone_digits", "company_norm",
        "source_channel", "source_detail", "web_form_id", "web_page_url", "web_submitted_at",
        "created_at", "updated_at",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(leads)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
    )


@app.get("/leads/{lead_id}")
def get_lead(lead_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Lead not found")
    return _row_to_dict(row)


@app.patch("/leads/{lead_id}")
def update_lead(lead_id: int, patch: LeadUpdate):
    fields, params = [], []
    if patch.status is not None:
        fields.append("status = ?")
        params.append(patch.status)
        fields.append("status_norm = ?")
        params.append(normalize_status(patch.status))
    if patch.owner is not None:
        fields.append("owner = ?")
        params.append(patch.owner)
    if patch.notes is not None:
        fields.append("notes = ?")
        params.append(patch.notes)
        src = extract_source(patch.notes)
        fields.append("source_channel = ?")
        params.append(src.channel)
        fields.append("source_detail = ?")
        params.append(src.detail)

    if not fields:
        raise HTTPException(400, "No fields to update")

    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(lead_id)

    with db_cursor() as cur:
        cur.execute(f"UPDATE leads SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(404, "Lead not found")

    return get_lead(lead_id)


# --------------------------------------------------------------- ingest ----

def _find_existing_lead(conn, email_norm: str, phone_digits: str):
    """Same-person match for ingest: exact normalized email first (strong
    signal), then exact phone digits (also strong -- unlike dedup scoring,
    ingest needs a decisive yes/no, so the system only auto-matches on
    near-certain identity signals rather than fuzzy name/company)."""
    if email_norm:
        row = conn.execute("SELECT * FROM leads WHERE email_norm = ?", (email_norm,)).fetchone()
        if row:
            return row
    if phone_digits:
        row = conn.execute(
            "SELECT * FROM leads WHERE phone_digits = ? AND phone_digits != ''", (phone_digits,)
        ).fetchone()
        if row:
            return row
    return None


@app.post("/leads/ingest")
def ingest_lead(payload: IngestPayload):
    email_norm = normalize_email(payload.email)
    phone_digits = normalize_phone_digits(payload.phone or "")
    company_norm = normalize_company(payload.company or "")
    first, last = "", ""
    display_name = build_display_name(first, last, payload.name or "")

    note_parts = [p for p in [payload.message, f"(via {payload.form_name})" if payload.form_name else None] if p]
    notes = " ".join(note_parts)
    src = extract_source(notes)

    conn = get_connection()
    existing = _find_existing_lead(conn, email_norm, phone_digits)

    if existing:
        with db_cursor() as cur:
            cur.execute(
                """UPDATE leads SET
                    notes = notes || ' | ' || ?,
                    source_channel = ?, source_detail = ?,
                    phone_number = COALESCE(NULLIF(phone_number, ''), ?),
                    phone_digits = COALESCE(NULLIF(phone_digits, ''), ?),
                    company_name = COALESCE(NULLIF(company_name, ''), ?),
                    company_norm = COALESCE(NULLIF(company_norm, ''), ?),
                    country = COALESCE(NULLIF(country, ''), ?),
                    country_norm = COALESCE(NULLIF(country_norm, ''), ?),
                    web_form_id = ?, web_page_url = ?, web_submitted_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    notes or "(form resubmission, no message)",
                    src.channel, src.detail,
                    payload.phone or "", phone_digits,
                    payload.company or "", company_norm,
                    payload.country or "", normalize_country(payload.country or ""),
                    # these three deliberately always overwrite (not COALESCE-guarded
                    # like the fields above) -- they track the *most recent* form
                    # touchpoint, so a repeat submission should update them, not
                    # keep whatever was captured on the first-ever submission
                    payload.form_id or "", payload.page_url or "", payload.submitted_at or "",
                    existing["id"],
                ),
            )
        conn.close()
        return {"action": "updated", "lead": get_lead(existing["id"])}

    conn.close()
    record_id = f"web_{payload.form_id or 'form'}_{email_norm or phone_digits}"
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO leads (
                record_id, first_name, last_name, full_name, company_name, email,
                phone_number, country, country_norm, status, lifecycle_stage, original_source,
                owner, create_date, notes, display_name, status_norm, email_norm,
                email_domain, phone_digits, company_norm, source_channel, source_detail,
                web_form_id, web_page_url, web_submitted_at
            ) VALUES (?, '', '', ?, ?, ?, ?, ?, ?, 'New', 'Lead', 'Website', 'Unassigned',
                      date('now'), ?, ?, 'New', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id) DO NOTHING""",
            (
                record_id, payload.name or "", payload.company or "", payload.email,
                payload.phone or "", payload.country or "", normalize_country(payload.country or ""),
                notes, display_name, email_norm, email_domain(payload.email),
                phone_digits, company_norm, src.channel, src.detail,
                payload.form_id or "", payload.page_url or "", payload.submitted_at or "",
            ),
        )
        new_id = cur.lastrowid

    return {"action": "created", "lead": get_lead(new_id)}


# --------------------------------------------------------- source extract -

class ExtractSourceRequest(BaseModel):
    notes: str
    original_source: Optional[str] = ""


@app.post("/leads/extract-source")
def extract_source_endpoint(payload: ExtractSourceRequest):
    """Exposes the source-extraction logic (used internally at load time and
    on PATCH .../notes) as a standalone endpoint too, so it can be tested
    directly against arbitrary free text."""
    result = extract_source(payload.notes, payload.original_source or "")
    return {"channel": result.channel, "detail": result.detail, "method": result.method}


# ------------------------------------------------------------- dedupe -----

@app.post("/leads/dedupe-candidates")
def dedupe_candidates(min_confidence: float = 0.5, limit: int = 200):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, display_name, email_norm, email_domain, phone_digits, company_norm FROM leads"
    ).fetchall()
    conn.close()

    leads = [
        DedupLead(r["id"], r["display_name"], r["email_norm"], r["email_domain"], r["phone_digits"], r["company_norm"])
        for r in rows
    ]
    groups = group_dedupe_candidates(leads, min_confidence=min_confidence)[:limit]

    conn = get_connection()

    def _lead_summary(lead_id: int) -> dict:
        row = conn.execute(
            "SELECT id, display_name, email, company_name, phone_number FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        return _row_to_dict(row)

    out = []
    for g in groups:
        out.append({
            "group_confidence": g.group_confidence,
            "members": [_lead_summary(lid) for lid in g.member_ids],
            "supporting_pairs": [
                {
                    "lead_id_a": e.lead_id_a,
                    "lead_id_b": e.lead_id_b,
                    "confidence": e.confidence,
                    "reason": e.reason,
                }
                for e in g.edges
            ],
        })
    conn.close()
    return {"count": len(out), "groups": out}


# ------------------------------------------------------------ dashboard ---

@app.get("/dashboard")
def dashboard():
    conn = get_connection()
    by_status = conn.execute(
        "SELECT status_norm AS status, COUNT(*) AS count FROM leads GROUP BY status_norm ORDER BY count DESC"
    ).fetchall()
    by_channel = conn.execute(
        "SELECT source_channel AS channel, COUNT(*) AS count FROM leads GROUP BY source_channel ORDER BY count DESC"
    ).fetchall()
    # top 10 -- these two fields have many more distinct values than
    # status/channel (35 countries, 10+ owners), so a full breakdown would
    # be a very long, low-signal chart. Top 10 keeps it readable and still
    # answers "where is most of our lead volume coming from" without
    # requiring pagination or a separate endpoint for what's still just
    # dashboard-level context, not a precise report.
    by_country = conn.execute(
        "SELECT country_norm AS country, COUNT(*) AS count FROM leads "
        "WHERE country_norm != '' GROUP BY country_norm ORDER BY count DESC LIMIT 10"
    ).fetchall()
    by_owner = conn.execute(
        "SELECT owner, COUNT(*) AS count FROM leads "
        "WHERE owner != '' GROUP BY owner ORDER BY count DESC LIMIT 10"
    ).fetchall()
    # leads created per month -- create_date was already normalized to
    # YYYY-MM-DD at load time (see normalize_date()), so substr(..., 1, 7)
    # cheaply gives a YYYY-MM bucket without needing a separate date library
    by_month = conn.execute(
        "SELECT substr(create_date, 1, 7) AS month, COUNT(*) AS count FROM leads "
        "WHERE create_date != '' GROUP BY month ORDER BY month ASC"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    conn.close()
    return {
        "total_leads": total,
        "by_status": [_row_to_dict(r) for r in by_status],
        "by_source_channel": [_row_to_dict(r) for r in by_channel],
        "by_country": [_row_to_dict(r) for r in by_country],
        "by_owner": [_row_to_dict(r) for r in by_owner],
        "leads_by_month": [_row_to_dict(r) for r in by_month],
    }


@app.get("/dashboard/drilldown")
def dashboard_drilldown(country: str, city: Optional[str] = None):
    """Return the next breakdown level for the dashboard's interactive chart."""
    conn = get_connection()
    if city is None:
        normalized_country = normalize_country(country)
        has_city_data = conn.execute(
            "SELECT 1 FROM leads WHERE country_norm = ? AND city != '' LIMIT 1",
            (normalized_country,),
        ).fetchone()
        if has_city_data:
            rows = conn.execute(
                "SELECT city AS label, COUNT(*) AS count FROM leads "
                "WHERE country_norm = ? AND city != '' "
                "GROUP BY city ORDER BY count DESC, label ASC LIMIT 10",
                (normalized_country,),
            ).fetchall()
            dimension = "City"
        else:
            rows = conn.execute(
                "SELECT status_norm AS label, COUNT(*) AS count FROM leads "
                "WHERE country_norm = ? GROUP BY status_norm "
                "ORDER BY count DESC, label ASC",
                (normalized_country,),
            ).fetchall()
            dimension = "Status"
    else:
        rows = conn.execute(
            "SELECT status_norm AS label, COUNT(*) AS count FROM leads "
            "WHERE country_norm = ? AND city = ? "
            "GROUP BY status_norm ORDER BY count DESC, label ASC",
            (normalize_country(country), "" if city == "Unknown" else city),
        ).fetchall()
        dimension = "Status"
    conn.close()
    return {
        "country": normalize_country(country),
        "city": city,
        "dimension": dimension,
        "items": [_row_to_dict(row) for row in rows],
    }


@app.get("/dashboard/view")
def dashboard_view():
    """Serves a simple HTML dashboard (bar charts via Chart.js) that fetches
    live data from GET /dashboard on load. A rendered chart is called out
    in the brief as a nice-to-have on top of the required JSON endpoint --
    this is that. Refresh the page (or click the button on it) to see
    updated numbers; it doesn't push live without a reload."""
    html_path = Path(__file__).resolve().parent / "static" / "dashboard.html"
    return FileResponse(html_path)


@app.get("/")
def root():
    return {"service": "Mini Lead Management System", "docs": "/docs"}
