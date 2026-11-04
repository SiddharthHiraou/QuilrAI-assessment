"""Task 3: detectors, and the streaming redactor's equivalence to one-shot."""

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tollgate.guardrail.patterns import MAX_SPAN, card_ok, luhn_ok
from tollgate.guardrail.redactor import PLACEHOLDER, StreamRedactor, redact


def stream(text, sizes):
    r = StreamRedactor()
    out, i, k = [], 0, 0
    while i < len(text):
        n = sizes[k % len(sizes)]
        out.append(r.feed(text[i:i + n]))
        i, k = i + n, k + 1
    out.append(r.flush())
    return "".join(out), r


# --- detectors -----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "reach me at priya.raman@example.com today",
    "first_last+tag@mail.co.uk",
    "card 4111 1111 1111 1111 on file",
    "card 4111-1111-1111-1111 on file",
    "card 4111111111111111 on file",
    "amex 378282246310005",
    "mastercard 2223003122003222",
    "ssn 123-45-6789",
    "ssn 123 45 6789",
    "call (415) 555-2671",
    "call 415-555-2671 or 415.555.2671",
    "call +1 415 555 2671",
    "key sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz",
    "aws AKIAIOSFODNN7EXAMPLE",
    "gh ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
])
def test_detected(text):
    assert PLACEHOLDER in redact(text)


@pytest.mark.parametrize("text", [
    "order 1234567890123456 shipped",          # 16 digits, fails Luhn
    "invoice 999-88-7777",                     # area 9xx is never issued
    "id 000-12-3456 and 666-12-3456",          # area 000 and 666 neither
    "ref 123-00-4567 and 123-45-0000",         # zero group, zero serial
    "mixed 123-45 6789",                       # separators must match
    "the date is 2024-01-02",
    "version 1.24.3 released",
    "server 10.0.0.12 port 8080",
    "total $1,234.56 due",
    "meet at 12:30 on 2024/05/06",
    "phone-ish 555-0100",                      # seven digits, no area code
    "bare digits 4155552671",                  # ten digits, no grouping
    "user@localhost",                          # no TLD
    "sk-short",
    "a gravatar hash 5e8ff9bf55ba3508199d22e984129be6",
])
def test_not_detected(text):
    assert redact(text) == text


def test_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert card_ok("4532-0151-1283-0366")
    assert not card_ok("9111111111111111")  # passes nothing: 9 isn't an issuer prefix


def test_phone_with_parens_is_fully_removed():
    # A \b anchor misses the "(" and leaves it behind: "([REDACTED]".
    assert redact("call (415) 555-2671 now") == "call [REDACTED] now"


def test_labeled_mode():
    out = redact("mail a.b@example.com or 123-45-6789", labeled=True)
    assert out == "mail [REDACTED:EMAIL] or [REDACTED:SSN]"


def test_found_counts_never_hold_values():
    r = StreamRedactor()
    r.feed("x@example.com and 4111 1111 1111 1111 ")
    r.flush()
    assert dict(r.found) == {"email": 1, "credit_card": 1}


# --- streaming equals one-shot -------------------------------------------------

CORPUS = (
    "Hi, it's Priya. My email is priya.raman@example.com and my card is 4111 1111 1111 1111. "
    "SSN 123-45-6789, call (415) 555-2671 or +1 206.555.0187. Order 1234567890123456 is not a "
    "card. Key: sk-proj-abcdefghijklmnopqrstuvwxyz0123 then AKIAIOSFODNN7EXAMPLE. Dates like "
    "2024-01-02 stay. Old card 4532-0151-1283-0366 was replaced.\n"
)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 16, 64, 1000])
def test_fixed_chunk_sizes(size):
    expected = redact(CORPUS * 3)
    assert stream(CORPUS * 3, [size])[0] == expected


def test_random_chunkings():
    expected = redact(CORPUS * 4)
    rng = random.Random(1234)
    for _ in range(200):
        sizes = [rng.randint(1, 40) for _ in range(50)]
        assert stream(CORPUS * 4, sizes)[0] == expected


def test_pii_split_at_every_possible_point():
    text = "card: 4111 1111 1111 1111!"
    expected = redact(text)
    for cut in range(1, len(text)):
        r = StreamRedactor()
        assert r.feed(text[:cut]) + r.feed(text[cut:]) + r.flush() == expected


# Built from the characters that matter most: digits, every separator, the
# email and key alphabets, and fragments of real PII so matches are common.
FRAGMENTS = ["4111", " 1111", "-", ".", "@", "example.com", "(415) ", "555-", "2671", "123-45-",
             "6789", "sk-", "proj-", "abcdefghijklmnopqrstuvwxyz", "AKIA", "IOSFODNN7EXAMPLE", "_",
             " ", "\n", "a", "Z", "9", "+1 ", "x@y.io", "0366", "4532-0151-1283-"]
texts = st.lists(st.one_of(st.sampled_from(FRAGMENTS), st.text(alphabet="0123456789 -.()@+_abAZ", max_size=8)),
                 max_size=60).map("".join)


@settings(max_examples=400, deadline=None)
@given(text=texts, sizes=st.lists(st.integers(min_value=1, max_value=30), min_size=1, max_size=40))
def test_property_stream_equals_one_shot(text, sizes):
    assert stream(text, sizes)[0] == redact(text)


@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=300), sizes=st.lists(st.integers(min_value=1, max_value=20), min_size=1, max_size=20))
def test_property_on_arbitrary_unicode(text, sizes):
    assert stream(text, sizes)[0] == redact(text)


# --- memory and latency ----------------------------------------------------------

def test_prose_is_held_back_by_at_most_a_word():
    r = StreamRedactor()
    out = r.feed("please process the refund for my order a")
    assert out == "please process the refund for my order "
    assert r.held == 1


@pytest.mark.parametrize("filler", ["7", "a", "a.", "1 ", "x-"])
def test_holdback_is_capped_on_adversarial_runs(filler):
    # Long runs that never let a family run end. Without the MAX_SPAN cap the
    # buffer would grow with the input.
    r = StreamRedactor()
    for _ in range(20_000):
        r.feed(filler)
    assert r.max_held <= MAX_SPAN


def test_buffer_stays_small_over_a_large_stream():
    r = StreamRedactor()
    total = 0
    for _ in range(2_000):
        total += len(r.feed(CORPUS))
    total += len(r.flush())
    assert total > 400_000  # ~0.5 MB through, minus what got redacted
    assert r.max_held <= 64
