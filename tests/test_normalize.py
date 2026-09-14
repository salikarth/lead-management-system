from app.normalize import (
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


def test_normalize_status_collapses_case_and_whitespace():
    assert normalize_status("New") == "New"
    assert normalize_status("new") == "New"
    assert normalize_status("NEW") == "New"
    assert normalize_status(" New") == "New"
    assert normalize_status("closed won") == "Closed Won"


def test_normalize_status_empty():
    assert normalize_status("") == "Unknown"
    assert normalize_status(None) == "Unknown"


def test_display_name_prefers_first_last_when_both_present():
    assert build_display_name("Yuki", "Aina", "") == "Yuki Aina"


def test_display_name_falls_back_to_full_name():
    assert build_display_name("", "", "Wei Ming Malik") == "Wei Ming Malik"


def test_display_name_handles_partial_name():
    assert build_display_name("Yuki", "", "") == "Yuki"


def test_display_name_no_name_at_all():
    assert build_display_name("", "", "") == "(no name)"


def test_split_full_name_two_tokens():
    assert split_full_name("Yuki Aina") == ("Yuki", "Aina")


def test_split_full_name_multi_token_last_name():
    assert split_full_name("Wei Ming Malik") == ("Wei", "Ming Malik")


def test_split_full_name_single_token():
    assert split_full_name("Cher") == ("Cher", "")


def test_split_full_name_empty():
    assert split_full_name("") == ("", "")


def test_normalize_email_lowercases():
    assert normalize_email("Y.Aina@SinghLogistics.io") == "y.aina@singhlogistics.io"


def test_email_domain_extraction():
    assert email_domain("a@example.com") == "example.com"
    assert email_domain("not-an-email") == ""


def test_phone_digits_strips_formatting():
    assert normalize_phone_digits("+86 138 2424 7912") == "8613824247912"
    assert normalize_phone_digits("(415) 555-0100") == "4155550100"


def test_normalize_company_strips_legal_suffixes():
    a = normalize_company("Chen Digital Inc.")
    b = normalize_company("Chen Digital, Inc")
    c = normalize_company("Chen Digital Co")
    assert a == b == c == "chen digital"


def test_normalize_company_handles_ampersand():
    assert normalize_company("Singh Logistics & Co") == "singh logistics"


def test_normalize_date_handles_multiple_formats():
    assert normalize_date("2026-06-02") == "2026-06-02"
    assert normalize_date("6/4/2026") == "2026-06-04"
    assert normalize_date("2026-05-20T00:00:00Z") == "2026-05-20"


def test_normalize_date_unparseable_returns_empty():
    assert normalize_date("not a date") == ""
    assert normalize_date("") == ""


def test_normalize_country_collapses_case():
    assert normalize_country("China") == "China"
    assert normalize_country("china") == "China"
    assert normalize_country("CHINA") == "China"
    assert normalize_country(" australia ") == "Australia"
