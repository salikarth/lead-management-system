import json
import os

import pytest

from app.source_extraction import extract_source


def test_event_qr_code():
    r = extract_source("Met him at the SFF booth, scanned our QR code")
    assert r.channel == "Event"


def test_organic_search():
    r = extract_source("Found us through organic google search then booked a demo")
    assert r.channel == "Organic Search"


def test_linkedin():
    r = extract_source("Connected on LinkedIn after I sent an InMail")
    assert r.channel == "LinkedIn"


def test_referral():
    r = extract_source("Referred by Michael Zhang, warm intro. Connected, sending proposal.")
    assert r.channel == "Referral"


def test_website_form():
    r = extract_source("Filled out the form on the contact page.")
    assert r.channel == "Website"


def test_manual_sales_cold_outreach():
    r = extract_source("Manually added by sales after a phone call from a cold outreach list.")
    assert r.channel == "Manual/Sales"


def test_paid_google_ad_is_not_misclassified_as_organic():
    r = extract_source("Booked a demo via the book-a-demo page after clicking a google ad.")
    # a paid ad click is explicitly not "organic" search -- this is the
    # kind of nuance a naive keyword-on-'google' rule would get wrong
    assert r.channel != "Organic Search"


def test_empty_notes_falls_back_to_original_source():
    r = extract_source("", original_source="Referrals")
    assert r.channel == "Referral"


def test_completely_empty_falls_back_to_other():
    r = extract_source("", original_source="")
    assert r.channel == "Other"
    assert r.method == "fallback"


def test_ambiguous_note_does_not_crash():
    r = extract_source("asdkjfh random text with no signal 12345")
    assert r.channel in {"Website", "Event", "LinkedIn", "Organic Search", "Referral", "Manual/Sales", "Other"}


def test_llm_enabled_toggles_with_gemini_key(monkeypatch):
    import importlib
    import app.source_extraction as se

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    importlib.reload(se)
    assert se.LLM_ENABLED is False

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    importlib.reload(se)
    assert se.LLM_ENABLED is True

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    importlib.reload(se)


def test_detail_drops_unrelated_trailing_activity_sentence():
    r = extract_source("Referred by Michael Zhang, warm intro. Connected, sending proposal.")
    assert r.channel == "Referral"
    assert "sending proposal" not in r.detail
    assert "Referred by Michael Zhang" in r.detail


def test_detail_keeps_only_the_source_sentence():
    r = extract_source("Met him at the SFF booth, scanned our QR code. Connected, sending proposal.")
    assert r.channel == "Event"
    assert r.detail == "Met him at the SFF booth, scanned our QR code"


def test_llm_not_called_when_text_is_empty_even_if_enabled(monkeypatch):
    import importlib
    import app.source_extraction as se

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    importlib.reload(se)
    assert se.LLM_ENABLED is True

    r = se.extract_source("", original_source="")
    assert r.method == "fallback"  # never reaches the LLM call path

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    importlib.reload(se)


class TestParseLlmJson:
    """Direct tests of _parse_llm_json() -- this is the part of the LLM path
    most likely to break, since it depends on the model returning the exact
    shape asked for, which isn't guaranteed."""

    def test_plain_json(self):
        from app.source_extraction import _parse_llm_json
        r = _parse_llm_json('{"channel": "Event", "detail": "Tech Conf 2026"}', "fallback text")
        assert r.channel == "Event"
        assert r.detail == "Tech Conf 2026"
        assert r.method == "llm"

    def test_json_wrapped_in_markdown_code_fence(self):
        from app.source_extraction import _parse_llm_json
        raw = '```json\n{"channel": "Referral", "detail": "Friend intro"}\n```'
        r = _parse_llm_json(raw, "fallback text")
        assert r.channel == "Referral"
        assert r.detail == "Friend intro"

    def test_unrecognized_channel_falls_back_to_other(self):
        from app.source_extraction import _parse_llm_json
        # model invents a channel not in the fixed list -- must be coerced
        r = _parse_llm_json('{"channel": "Paid Ads", "detail": "some ad"}', "fallback text")
        assert r.channel == "Other"

    def test_missing_detail_field_uses_fallback_text(self):
        from app.source_extraction import _parse_llm_json
        r = _parse_llm_json('{"channel": "Website"}', "original note text")
        assert r.detail == "original note text"

    def test_malformed_json_raises(self):
        from app.source_extraction import _parse_llm_json
        import pytest
        with pytest.raises(json.JSONDecodeError):
            _parse_llm_json("this is not json at all", "fallback text")


class TestGeminiBased:
    """_gemini_based() is mocked at the HTTP layer -- no real network call --
    so these run offline and don't depend on a live API key."""

    def test_successful_call_parses_response(self, monkeypatch):
        import app.source_extraction as se
        monkeypatch.setattr(se, "GEMINI_API_KEY", "dummy")

        fake_response_body = json.dumps({
            "candidates": [{"content": {"parts": [
                {"text": '{"channel": "Event", "detail": "Tech Summit"}'}
            ]}}]
        }).encode()

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return fake_response_body

        monkeypatch.setattr(se.urllib.request, "urlopen", lambda req, timeout=15: FakeResp())

        r = se._gemini_based("some ambiguous note")
        assert r.channel == "Event"
        assert r.detail == "Tech Summit"
        assert r.method == "llm"

    def test_network_error_falls_back_gracefully(self, monkeypatch):
        import app.source_extraction as se
        monkeypatch.setattr(se, "GEMINI_API_KEY", "dummy")

        def raise_error(req, timeout=15):
            raise OSError("network unreachable")

        monkeypatch.setattr(se.urllib.request, "urlopen", raise_error)

        r = se._gemini_based("some note")
        assert r.channel == "Other"
        assert r.method == "fallback"

    def test_malformed_api_response_falls_back_gracefully(self, monkeypatch):
        import app.source_extraction as se
        monkeypatch.setattr(se, "GEMINI_API_KEY", "dummy")

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"unexpected": "shape"}'  # missing "candidates"

        monkeypatch.setattr(se.urllib.request, "urlopen", lambda req, timeout=15: FakeResp())

        r = se._gemini_based("some note")
        assert r.method == "fallback"  # KeyError caught, doesn't crash


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="Real Gemini integration test -- set GEMINI_API_KEY to run it. "
           "Skipped by default so CI / other contributors don't need a key "
           "and so the test suite doesn't consume API quota on every run.",
)
class TestGeminiLiveIntegration:
    """Real, non-mocked calls to the Gemini API. Deliberately NOT part of
    the default test run (see skipif above) -- these hit a live network
    service, so they're slower, cost quota, and can fail for reasons
    unrelated to the code (rate limits, model changes, transient network
    issues). Run explicitly with: GEMINI_API_KEY=... pytest -m ""
    (or just `pytest` once the env var is set, since skipif checks it)."""

    def test_live_call_returns_a_valid_channel(self):
        import importlib
        import app.source_extraction as se
        importlib.reload(se)  # pick up the real key from the environment

        r = se.extract_source(
            "Someone from their team stumbled onto us while browsing around "
            "during their coffee break at a big tech gathering downtown"
        )
        if r.method == "fallback":
            pytest.skip("Gemini live API unavailable or quota exhausted")
        # the test can't assert the exact channel (that's the model's call, not
        # the test's) -- but it CAN assert the contract the code promises: a
        # valid channel from the fixed list, via the llm method, no crash
        assert r.channel in se.CHANNELS
        assert r.method == "llm"
        assert len(r.detail) > 0

    def test_live_call_handles_a_clearly_unclassifiable_note(self):
        import importlib
        import app.source_extraction as se
        importlib.reload(se)

        r = se.extract_source("Nice guy, seems busy, will follow up Monday")
        if r.method == "fallback":
            pytest.skip("Gemini live API unavailable or quota exhausted")
        assert r.channel in se.CHANNELS
        assert r.method == "llm"
