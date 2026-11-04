"""PII redaction on tool results, reusing the task 3 detectors.

The gateway sees everything a tool returns before the agent does, which makes
it the right place to stop a customer record's email and phone from flowing
into a model's context (and from there into logs, traces and other users'
answers). Which roles get redacted results is policy; by default admins see
raw data and everyone else doesn't.
"""

from collections import Counter

from tollgate.guardrail.redactor import StreamRedactor

META_KEY = "tollgate.dev/redactions"


def _walk(value, redactor):
    if isinstance(value, str):
        return redactor.redact_all(value)
    if isinstance(value, list):
        return [_walk(v, redactor) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, redactor) for k, v in value.items()}
    return value


def redact_tool_result(result):
    """Returns (new_result, counts). Keys are left alone, only values change."""
    text_pass, structured_pass = StreamRedactor(), StreamRedactor()
    out = dict(result)
    content = result.get("content")
    if isinstance(content, list):
        out["content"] = [
            {**item, "text": text_pass.redact_all(item["text"])}
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            else item
            for item in content
        ]
    if "structuredContent" in result:
        out["structuredContent"] = _walk(result["structuredContent"], structured_pass)
    # The text block is usually the structured result serialised, so the same
    # email shows up in both. Count it once.
    counts = Counter(max(text_pass.found, structured_pass.found, key=lambda c: sum(c.values())))
    if counts:
        meta = dict(out.get("_meta") or {})
        meta[META_KEY] = dict(counts)
        out["_meta"] = meta
    return out, counts
