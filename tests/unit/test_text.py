import pytest

from fanvaaz.text import clean, contains_pii, prepare, redact_pii


@pytest.mark.parametrize(
    "raw",
    [
        "call 9876543210 for price",
        "call +91 98765 43210 now",
        "call +91-9876543210",
        "whatsapp 09876543210",
        "mail me at deals.seller@example.com",
    ],
)
def test_pii_is_redacted(raw):
    out = redact_pii(raw)
    assert not contains_pii(out)
    assert "[phone]" in out or "[email]" in out


@pytest.mark.parametrize(
    "safe", ["1200mm sweep, 28W", "bill went from 2400 to 1900", "speed 5 of 5", "2026 model"]
)
def test_numbers_that_are_not_phones_survive(safe):
    assert redact_pii(safe) == safe


def test_clean_strips_html_and_entities():
    assert clean("OnePlus&#39;s <b>best</b>   phone") == "OnePlus's best phone"


def test_prepare_cleans_then_redacts():
    assert prepare("Call <i>9876543210</i>") == "Call [phone]"
