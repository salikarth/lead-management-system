import pytest
from fastapi.testclient import TestClient

from app.db import init_db, db_cursor


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # isolate each test run on a fresh throwaway DB file
    import app.db as db_module
    test_db = tmp_path / "test_leads.db"
    monkeypatch.setattr(db_module, "DB_PATH", test_db)

    from app.main import app
    init_db(reset=True)

    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO leads (
                record_id, first_name, last_name, display_name, email, email_norm,
                email_domain, phone_number, phone_digits, company_name, company_norm,
                country, country_norm, city, status, status_norm, owner, notes, source_channel, source_detail
            ) VALUES
            ('r1','Yuki','Aina','Yuki Aina','y.aina@singh.io','y.aina@singh.io','singh.io',
             '+86 138 2424 7912','8613824247912','Singh Logistics','singh logistics','China','China','',
             'New','New','Marcus Wong','Scanned QR at booth','Event','SFF booth'),
            ('r2','Wei','Malik','Wei Malik','wei@delta.co','wei@delta.co','delta.co',
             '+33 6 15 68 78 25','33615687825','Delta Inc','delta','France','France','',
             'Qualified','Qualified','Rohan Mehta','Referred by a friend','Referral','friend referral')
            """
        )

    return TestClient(app)


def test_list_leads(client):
    r = client.get("/leads")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2


def test_filter_by_status(client):
    r = client.get("/leads", params={"status": "new"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["leads"][0]["display_name"] == "Yuki Aina"


def test_filter_by_owner(client):
    r = client.get("/leads", params={"owner": "Rohan Mehta"})
    assert r.json()["total"] == 1
    assert r.json()["leads"][0]["display_name"] == "Wei Malik"


def test_pagination_offset_skips_rows(client):
    page1 = client.get("/leads", params={"limit": 1, "offset": 0}).json()
    page2 = client.get("/leads", params={"limit": 1, "offset": 1}).json()
    assert page1["total"] == page2["total"] == 2
    assert page1["leads"][0]["id"] != page2["leads"][0]["id"]


def test_pagination_limit_over_max_rejected(client):
    r = client.get("/leads", params={"limit": 100000})
    assert r.status_code == 422


def test_filter_by_q_search(client):
    r = client.get("/leads", params={"q": "Delta"})
    assert r.json()["total"] == 1


def test_filter_by_country_is_case_insensitive(client):
    # regression test: seed data has both 'China'/'china' style casing
    # variants for ~27 countries; the filter must not require an exact
    # case match against whatever casing happens to be in that row
    r_lower = client.get("/leads", params={"country": "china"})
    r_title = client.get("/leads", params={"country": "China"})
    r_upper = client.get("/leads", params={"country": "CHINA"})
    assert r_lower.json()["total"] == r_title.json()["total"] == r_upper.json()["total"] == 1


def test_get_single_lead(client):
    r = client.get("/leads")
    lead_id = r.json()["leads"][0]["id"]
    detail = client.get(f"/leads/{lead_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == lead_id


def test_get_missing_lead_404(client):
    r = client.get("/leads/99999")
    assert r.status_code == 404


def test_patch_lead_status(client):
    lead_id = client.get("/leads").json()["leads"][0]["id"]
    r = client.patch(f"/leads/{lead_id}", json={"status": "contacted"})
    assert r.status_code == 200
    assert r.json()["status_norm"] == "Contacted"


def test_patch_lead_owner(client):
    lead_id = client.get("/leads").json()["leads"][0]["id"]
    r = client.patch(f"/leads/{lead_id}", json={"owner": "New Owner"})
    assert r.json()["owner"] == "New Owner"


def test_patch_ignores_unauthorized_fields(client):
    lead = client.get("/leads").json()["leads"][0]
    original_email = lead["email"]
    r = client.patch(f"/leads/{lead['id']}", json={
        "status": "New",
        "email": "hacked@evil.com",
        "id": 99999,
    })
    assert r.status_code == 200
    assert r.json()["email"] == original_email  # unauthorized field silently ignored
    assert r.json()["id"] == lead["id"]  # id cannot be overwritten


def test_patch_notes_recomputes_source(client):
    lead_id = client.get("/leads").json()["leads"][0]["id"]
    r = client.patch(f"/leads/{lead_id}", json={"notes": "Connected on LinkedIn"})
    assert r.json()["source_channel"] == "LinkedIn"


def test_patch_missing_lead_404(client):
    r = client.patch("/leads/99999", json={"status": "New"})
    assert r.status_code == 404


def test_patch_no_fields_400(client):
    lead_id = client.get("/leads").json()["leads"][0]["id"]
    r = client.patch(f"/leads/{lead_id}", json={})
    assert r.status_code == 400


def test_ingest_creates_new_lead(client):
    r = client.post("/leads/ingest", json={
        "name": "Brand New Person",
        "email": "brand.new@nowhere.com",
        "phone": "+1 555 000 1111",
        "company": "Nowhere Inc",
        "country": "USA",
        "message": "Filled out the contact form",
    })
    assert r.status_code == 200
    assert r.json()["action"] == "created"
    assert client.get("/leads").json()["total"] == 3


def test_ingest_matches_existing_lead_by_email(client):
    r = client.post("/leads/ingest", json={
        "name": "Yuki Aina",
        "email": "y.aina@singh.io",  # same email as seeded r1
        "message": "Following up again",
    })
    assert r.status_code == 200
    assert r.json()["action"] == "updated"
    assert client.get("/leads").json()["total"] == 2  # no new row created


def test_ingest_matches_existing_lead_by_phone(client):
    # different email, but phone digits match seeded r1 exactly
    r = client.post("/leads/ingest", json={
        "name": "Yuki Aina",
        "email": "a-completely-different-email@nowhere.com",
        "phone": "+86 138 2424 7912",
        "message": "Following up",
    })
    assert r.json()["action"] == "updated"
    assert client.get("/leads").json()["total"] == 2


def test_ingest_backfills_blank_fields_without_overwriting_existing(client):
    lead_id = client.get("/leads", params={"q": "Yuki"}).json()["leads"][0]["id"]
    before = client.get(f"/leads/{lead_id}").json()
    assert before["city"] == ""  # seed fixture never set city

    r = client.post("/leads/ingest", json={
        "email": "y.aina@singh.io",  # matches r1
        "company": "Should Not Overwrite",  # r1 already has a company
    })
    after = r.json()["lead"]
    assert after["company_name"] == "Singh Logistics"  # existing value preserved


def test_ingest_appends_notes_rather_than_overwriting(client):
    r = client.post("/leads/ingest", json={
        "email": "y.aina@singh.io",
        "message": "Second message from repeat visit",
    })
    notes = r.json()["lead"]["notes"]
    assert "Scanned QR at booth" in notes  # original note preserved
    assert "Second message from repeat visit" in notes  # new note appended


def test_ingest_stores_web_form_metadata(client):
    r = client.post("/leads/ingest", json={
        "form_id": "form_demo_request",
        "page_url": "/blog",
        "submitted_at": "2026-06-12T18:17:00Z",
        "name": "Metadata Test",
        "email": "metadata.test@nowhere.com",
        "message": "Checking metadata storage",
    })
    lead = r.json()["lead"]
    assert lead["web_form_id"] == "form_demo_request"
    assert lead["web_page_url"] == "/blog"
    assert lead["web_submitted_at"] == "2026-06-12T18:17:00Z"


def test_ingest_updates_web_metadata_to_latest_touchpoint(client):
    client.post("/leads/ingest", json={
        "page_url": "/blog", "email": "repeat.visitor@nowhere.com", "name": "Repeat Visitor",
    })
    r = client.post("/leads/ingest", json={
        "page_url": "/pricing", "email": "repeat.visitor@nowhere.com", "name": "Repeat Visitor",
    })
    assert r.json()["action"] == "updated"
    assert r.json()["lead"]["web_page_url"] == "/pricing"


def test_dedupe_candidates_endpoint(client):
    r = client.post("/leads/dedupe-candidates", params={"min_confidence": 0.5})
    assert r.status_code == 200
    assert "groups" in r.json()


def test_extract_source_endpoint(client):
    r = client.post("/leads/extract-source", json={"notes": "Connected on LinkedIn after I sent an InMail"})
    assert r.status_code == 200
    assert r.json()["channel"] == "LinkedIn"


def test_dashboard_endpoint(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["total_leads"] == 2
    assert len(body["by_status"]) == 2


def test_dashboard_includes_country_owner_and_time_breakdowns(client):
    r = client.get("/dashboard")
    body = r.json()
    assert "by_country" in body
    assert "by_owner" in body
    assert "leads_by_month" in body
    countries = {d["country"] for d in body["by_country"]}
    assert "China" in countries  # seeded lead r1 is China
    owners = {d["owner"] for d in body["by_owner"]}
    assert "Marcus Wong" in owners  # seeded lead r1's owner


def test_dashboard_drilldown_country_to_city_and_status(client):
    city_response = client.get("/dashboard/drilldown", params={"country": "china"})
    assert city_response.status_code == 200
    city_body = city_response.json()
    assert city_body["dimension"] == "Status"
    assert city_body["country"] == "China"
    assert city_body["items"] == [{"label": "New", "count": 1}]


def test_dashboard_view_serves_html(client):
    r = client.get("/dashboard/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Chart" in r.text  # references Chart.js


def test_export_returns_csv(client):
    r = client.get("/leads/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "Yuki Aina" in r.text


def test_export_respects_filters(client):
    r = client.get("/leads/export", params={"status": "new"})
    assert r.status_code == 200
    assert "Yuki Aina" in r.text
    assert "Wei Malik" not in r.text
