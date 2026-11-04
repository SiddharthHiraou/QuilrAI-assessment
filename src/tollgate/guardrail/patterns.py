"""What counts as sensitive.

Every detector is bounded: fixed quantifiers, no unbounded + or *. That's what
lets the stream redactor promise a hard cap on how much text it ever holds
back (see MAX_SPAN). Each one also uses single-character lookarounds for its
edges instead of \\b, because \\b doesn't fire before "(" and phone numbers
start with one.

Two character families matter to the redactor:
  WORD     emails and API keys are built only from these
  NUMERIC  cards, SSNs and phone numbers are built only from these
A match can never cross a character outside its family. The redactor uses
that to work out how much of the tail could still turn into a match.
"""

import re
import string
from dataclasses import dataclass

WORD = frozenset(string.ascii_letters + string.digits + "._%+-@_")
NUMERIC = frozenset(string.digits + " -.()+")


def luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def card_ok(text):
    digits = "".join(ch for ch in text if ch.isdigit())
    # IIN ranges that actually issue cards (Amex/Diners 3, Visa 4, MC 2 and 5,
    # Discover 6). Cuts down on order numbers that happen to pass Luhn.
    return 13 <= len(digits) <= 19 and digits[0] in "23456" and luhn_ok(digits)


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: str
    max_len: int
    check: object = None


_NOT_WORD_BEFORE = r"(?<![A-Za-z0-9._%+@_-])"
_NOT_ALNUM_BEFORE = r"(?<![A-Za-z0-9_])"
_NOT_ALNUM_AFTER = r"(?![A-Za-z0-9_])"

DETECTORS = [
    Detector(
        "email",
        _NOT_WORD_BEFORE
        + r"[A-Za-z0-9._%+_-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,4}[A-Za-z]{2,24}(?![A-Za-z0-9-])",
        max_len=64 + 1 + 4 * 64 + 24,
    ),
    Detector(
        "api_key",
        r"(?<![A-Za-z0-9_-])(?:"
        r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,200}"   # OpenAI, Anthropic
        r"|AKIA[0-9A-Z]{16}"                         # AWS access key id
        r"|gh[pousr]_[A-Za-z0-9]{36,200}"            # GitHub
        r"|xox[abposr]-[A-Za-z0-9-]{10,200}"         # Slack
        r"|AIza[0-9A-Za-z_-]{35}"                    # Google
        r")(?![A-Za-z0-9_-])",
        max_len=210,
    ),
    Detector(
        "credit_card",
        _NOT_ALNUM_BEFORE + r"(?:[0-9][ -]?){12,18}[0-9]" + _NOT_ALNUM_AFTER,
        max_len=19 + 18,
        check=card_ok,
    ),
    Detector(
        "ssn",
        # Same separator both times, and none of the ranges SSA never issues
        # (area 000, 666, 900-999; group 00; serial 0000). Bare nine-digit runs
        # aren't matched, there are far too many of those that aren't SSNs.
        _NOT_ALNUM_BEFORE
        + r"(?!000|666|9[0-9]{2})[0-9]{3}(?P<ssn_sep>[- ])(?!00)[0-9]{2}(?P=ssn_sep)(?!0000)[0-9]{4}"
        + _NOT_ALNUM_AFTER,
        max_len=11,
    ),
    Detector(
        "phone",
        r"(?<![A-Za-z0-9_+])(?:\+?1[ .-]?)?"
        r"(?:\([2-9][0-9]{2}\) ?|[2-9][0-9]{2}[ .-])[2-9][0-9]{2}[ .-][0-9]{4}"
        + _NOT_ALNUM_AFTER,
        max_len=3 + 6 + 8,
    ),
]

# One alternation, tried left to right at each position, so there is a single
# scan with one well-defined notion of "the next match". Order matters only
# where alternatives could start at the same place.
COMBINED = re.compile("|".join(f"(?P<{d.kind}>{d.pattern})" for d in DETECTORS))
BY_KIND = {d.kind: d for d in DETECTORS}

# Longest stretch a single attempt at one position can read: the longest match
# plus one character of lookahead. Anything further back than this from the
# end of the buffer can no longer change.
MAX_SPAN = max(d.max_len for d in DETECTORS) + 1
