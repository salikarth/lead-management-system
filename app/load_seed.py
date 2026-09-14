"""Load data/leads_seed.csv into SQLite, applying normalization.

Run standalone: python -m app.load_seed
"""

import csv
from pathlib import Path

from app.db import init_db, db_cursor
from app.normalize import (
    clean_str,
    normalize_status,
    build_display_name,
    split_full_name,
    normalize_email,
    email_domain,
    normalize_phone_digits,
    normalize_company,
    normalize_date,
    normalize_country,
)
from app.source_extraction import extract_source

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "leads_seed.csv"

INSERT_SQL = """
INSERT INTO leads (
    record_id, first_name, last_name, full_name, job_title, company_name,
    email, phone_number, country, country_norm, city, status, lifecycle_stage,
    original_source, original_source_drilldown, owner, create_date,
    last_modified_date, notes, lead_score,
    display_name, status_norm, email_norm, email_domain, phone_digits,
    company_norm, source_channel, source_detail
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(record_id) DO NOTHING
"""


def load_seed(reset: bool = True) -> int:
    init_db(reset=reset)
    count = 0
    with open(SEED_PATH, newline="", encoding="utf-8") as f, db_cursor() as cur:
        reader = csv.DictReader(f)
        for row in reader:
            first, last, full = row["First Name"], row["Last Name"], row["Full Name"]
            notes = clean_str(row["Notes"])
            original_source = clean_str(row["Original Source"])
            src = extract_source(notes, original_source)

            # if the row only has Full Name (no First/Last at all), derive
            # first/last by splitting it so the first_name/last_name columns
            # aren't left empty for the ~5% of rows shaped this way -- the
            # original Full Name is still kept as-is in `full_name`
            stored_first, stored_last = clean_str(first), clean_str(last)
            if not stored_first and not stored_last and clean_str(full):
                stored_first, stored_last = split_full_name(full)

            cur.execute(
                INSERT_SQL,
                (
                    clean_str(row["Record ID"]),
                    stored_first,
                    stored_last,
                    clean_str(full),
                    clean_str(row["Job Title"]),
                    clean_str(row["Company Name"]),
                    clean_str(row["Email"]),
                    clean_str(row["Phone Number"]),
                    clean_str(row["Country/Region"]),
                    normalize_country(row["Country/Region"]),
                    clean_str(row["City"]),
                    clean_str(row["Lead Status"]) or "Unknown",
                    clean_str(row["Lifecycle Stage"]),
                    original_source,
                    clean_str(row["Original Source Drill-Down 1"]),
                    clean_str(row["Contact Owner"]),
                    normalize_date(row["Create Date"]),
                    normalize_date(row["Last Modified Date"]),
                    notes,
                    clean_str(row["Lead Score"]),
                    build_display_name(first, last, full),
                    normalize_status(row["Lead Status"]),
                    normalize_email(row["Email"]),
                    email_domain(row["Email"]),
                    normalize_phone_digits(row["Phone Number"]),
                    normalize_company(row["Company Name"]),
                    src.channel,
                    src.detail,
                ),
            )
            count += 1
    return count


if __name__ == "__main__":
    n = load_seed(reset=True)
    print(f"Loaded {n} leads into {Path('data/leads.db').resolve()}")
