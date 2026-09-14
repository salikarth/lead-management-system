"""SQLite storage layer for the lead management system.

This project uses SQLite (via the stdlib sqlite3 module) rather than Postgres because this
is a scoped take-home: SQLite needs no running server, ships a single file
that's trivial to inspect/reset, and is more than adequate for ~2,000 rows.
The schema is intentionally close to the source CSV columns (snake_cased) so
loading is a near 1:1 mapping, with a few derived/normalized columns added
for search and dedup.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT UNIQUE,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT,
    job_title TEXT,
    company_name TEXT,
    email TEXT,
    phone_number TEXT,
    country TEXT,
    country_norm TEXT,
    city TEXT,
    status TEXT,
    lifecycle_stage TEXT,
    original_source TEXT,
    original_source_drilldown TEXT,
    owner TEXT,
    create_date TEXT,
    last_modified_date TEXT,
    notes TEXT,
    lead_score TEXT,

    -- populated only for leads created/touched via POST /leads/ingest --
    -- website form metadata that has no equivalent column in the original
    -- HubSpot-style CSV export, so it gets its own fields rather than being
    -- silently dropped or buried inside notes
    web_form_id TEXT,
    web_page_url TEXT,
    web_submitted_at TEXT,

    -- derived / normalized fields used for search, filtering and dedup
    display_name TEXT,
    status_norm TEXT,
    email_norm TEXT,
    email_domain TEXT,
    phone_digits TEXT,
    company_norm TEXT,
    source_channel TEXT,
    source_detail TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status_norm);
CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner);
CREATE INDEX IF NOT EXISTS idx_leads_country ON leads(country_norm);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email_norm);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_digits);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_norm);

CREATE TABLE IF NOT EXISTS dedupe_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id_a INTEGER NOT NULL,
    lead_id_b INTEGER NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT,
    method TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lead_id_a, lead_id_b)
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db(reset: bool = False):
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
