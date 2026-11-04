"""PII redaction over text that arrives in pieces.

The constraint: output has to be byte-identical to redacting the whole text in
one go, for every possible way the provider might chunk it, without ever
holding the whole response.

The idea is a "settled" point in the buffer. A regex attempt at position p
only ever reads characters from its own family (see patterns.py), plus one
character of lookahead. So an attempt at p can only change when more text
arrives if every character from p to the end of the buffer is in that family.
Walk back from the end over WORD characters, and separately over NUMERIC
ones; whichever run reaches further back marks where text stops being
settled. Before that point every match is final and can be redacted and sent.

Prose settles almost immediately: a space ends the WORD run and a letter ends
the NUMERIC run, so what's held back is usually the last partial word. Long
unbroken runs (base64, a wall of digits) are capped by MAX_SPAN, since nothing
more than MAX_SPAN characters back from the end can be read by any attempt.
"""

from collections import Counter

from tollgate.guardrail.patterns import BY_KIND, COMBINED, MAX_SPAN, NUMERIC, WORD

PLACEHOLDER = "[REDACTED]"

# Lookbehinds in patterns.py are one character wide, so one character of
# already-sent text is all the left context the next scan needs.
_CONTEXT = 1


def _tail_run_start(buf, floor, family):
    i = len(buf)
    while i > floor and buf[i - 1] in family:
        i -= 1
    return i


class StreamRedactor:
    def __init__(self, labeled=False):
        self.labeled = labeled
        self.found = Counter()
        self.max_held = 0
        self._buf = ""
        self._done = 0  # index in _buf up to which text has been emitted

    def _replacement(self, match):
        kind = match.lastgroup
        check = BY_KIND[kind].check
        if check is not None and not check(match.group()):
            return match.group()
        self.found[kind] += 1
        return f"[REDACTED:{kind.upper()}]" if self.labeled else PLACEHOLDER

    def _emit(self, settled):
        buf, out, pos = self._buf, [], self._done
        for m in COMBINED.finditer(buf, pos):
            if m.start() >= settled:
                break
            # A match that starts before the settled point is final even if it
            # runs past it: its own run ended inside the buffer.
            out.append(buf[pos:m.start()])
            out.append(self._replacement(m))
            pos = m.end()
        end = max(pos, settled)
        out.append(buf[pos:end])
        self._done = end
        return "".join(out)

    def feed(self, text):
        if not text:
            return ""
        self._buf += text
        buf, done = self._buf, self._done
        settled = min(_tail_run_start(buf, done, WORD), _tail_run_start(buf, done, NUMERIC))
        settled = max(settled, len(buf) - MAX_SPAN)
        out = self._emit(settled)

        keep_from = max(0, self._done - _CONTEXT)
        self._buf = self._buf[keep_from:]
        self._done -= keep_from
        self.max_held = max(self.max_held, len(self._buf) - self._done)
        return out

    def flush(self):
        """End of stream: everything left is settled."""
        out = self._emit(len(self._buf))
        self._buf, self._done = "", 0
        return out

    def redact_all(self, text):
        return COMBINED.sub(self._replacement, text)

    @property
    def held(self):
        return len(self._buf) - self._done


def redact(text, labeled=False):
    """One-shot redaction. The stream redactor must always agree with this."""
    return StreamRedactor(labeled=labeled).redact_all(text)
