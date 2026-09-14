from app.db import init_db, get_connection
from app.load_seed import load_seed


def test_load_seed_backfills_first_last_from_full_name_only_rows():
    load_seed(reset=True)
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE first_name = '' AND last_name = '' AND full_name != ''"
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_load_seed_populates_country_norm_for_every_row_with_a_country():
    load_seed(reset=True)
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE country != '' AND country_norm = ''"
    ).fetchone()[0]
    conn.close()
    assert n == 0
