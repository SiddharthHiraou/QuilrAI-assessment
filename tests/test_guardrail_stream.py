"""Task 3: provider stream codecs, and the gateway's streaming path on real sockets."""

import json
import tempfile
import time

import httpx2
import pytest

from tollgate.guardrail.stream import AnthropicMessages, OpenAIChat, redact_openai_completion
from tollgate.llm_gateway.app import create_app
from tollgate.llm_gateway.config import Config
from tollgate.mock_llm.app import PII_REPLY
from tollgate.mock_llm.app import create_app as mock_provider
from tollgate.router.router import Provider
from tollgate.sse import Event, Parser
from tollgate.testing import serving


def openai_chunk(content=None, index=0, finish=None, role=None):
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return Event(data=json.dumps({"id": "c1", "object": "chat.completion.chunk", "created": 1,
                                  "model": "m", "choices": [{"index": index, "delta": delta,
                                                             "finish_reason": finish}]}))


def text_of(events, index=0):
    out = ""
    for e in events:
        if e.data.startswith("{"):
            for c in json.loads(e.data).get("choices", []):
                if c.get("index", 0) == index:
                    out += (c.get("delta") or {}).get("content") or ""
    return out


def run(codec, events):
    out = []
    for e in events:
        out.extend(codec.on_event(e))
    out.extend(codec.on_end())
    return out


# --- OpenAI codec ----------------------------------------------------------------

def test_card_split_across_chunks():
    pieces = ["my card is 4111", " 1111 111", "1 1111, ok?"]
    events = [openai_chunk(role="assistant", content="")] + [openai_chunk(p) for p in pieces]
    events += [openai_chunk(finish="stop"), Event(data="[DONE]")]
    out = run(OpenAIChat(), events)
    assert text_of(out) == "my card is [REDACTED], ok?"
    assert out[-1].data == "[DONE]"


def test_held_text_is_flushed_before_finish():
    # The stream ends mid-word; that word must arrive, and before finish_reason.
    events = [openai_chunk("see you at noo"), openai_chunk("n", finish="stop"), Event(data="[DONE]")]
    out = run(OpenAIChat(), events)
    assert text_of(out) == "see you at noon"
    finish = [json.loads(e.data) for e in out if e.data.startswith("{")][-1]
    assert finish["choices"][0]["finish_reason"] == "stop"


def test_flush_when_provider_never_sends_finish_reason():
    out = run(OpenAIChat(), [openai_chunk("email a@example.co"), openai_chunk("m"), Event(data="[DONE]")])
    assert text_of(out) == "email [REDACTED]"
    assert out[-1].data == "[DONE]"


def test_interleaved_choices_keep_separate_state():
    # n=2: choice 0 and 1 interleave. One shared buffer would glue them together.
    events = [openai_chunk("card 4111 1111 ", 0), openai_chunk("hello ", 1),
              openai_chunk("1111 1111", 0), openai_chunk("world", 1),
              openai_chunk(finish="stop", index=0), openai_chunk(finish="stop", index=1),
              Event(data="[DONE]")]
    out = run(OpenAIChat(), events)
    assert text_of(out, 0) == "card [REDACTED]"
    assert text_of(out, 1) == "hello world"


def test_usage_chunk_dropped_unless_client_asked():
    usage = Event(data=json.dumps({"id": "c1", "choices": [], "usage": {"total_tokens": 42}}))
    events = [openai_chunk("hi", finish="stop"), usage, Event(data="[DONE]")]
    hidden = OpenAIChat(keep_usage_chunk=False)
    assert all('"usage"' not in e.data for e in run(hidden, events))
    assert hidden.usage == {"total_tokens": 42}
    kept = run(OpenAIChat(keep_usage_chunk=True), events)
    assert any('"usage"' in e.data for e in kept)


def test_comments_and_unknown_payloads_pass_through():
    events = [Event(comment=" ping"), Event(data="not json"), Event(data="[DONE]")]
    out = run(OpenAIChat(), events)
    assert [e.comment for e in out][:1] == [" ping"]
    assert out[1].data == "not json"


def test_non_streaming_completion_is_redacted():
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": PII_REPLY}}]}
    out = redact_openai_completion(body)
    assert "4111" not in json.dumps(out) and "priya.raman" not in json.dumps(out)
    assert body["choices"][0]["message"]["content"] == PII_REPLY  # input not mutated


# --- Anthropic codec ---------------------------------------------------------------

def anthropic(kind, **fields):
    return Event(event=kind, data=json.dumps({"type": kind, **fields}))


def test_anthropic_text_deltas():
    events = [
        anthropic("message_start", message={"id": "m1"}),
        anthropic("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        anthropic("content_block_delta", index=0, delta={"type": "text_delta", "text": "ssn 123-"}),
        anthropic("content_block_delta", index=0, delta={"type": "text_delta", "text": "45-678"}),
        anthropic("content_block_delta", index=0, delta={"type": "text_delta", "text": "9"}),
        anthropic("content_block_stop", index=0),
        anthropic("content_block_start", index=1, content_block={"type": "tool_use", "name": "lookup"}),
        anthropic("content_block_delta", index=1,
                  delta={"type": "input_json_delta", "partial_json": '{"q": "a@b.com"}'}),
        anthropic("content_block_stop", index=1),
        anthropic("message_stop"),
    ]
    out = run(AnthropicMessages(), events)
    text = "".join(json.loads(e.data)["delta"].get("text", "") for e in out if e.event == "content_block_delta")
    assert text == "ssn [REDACTED]"
    stop_at = next(i for i, e in enumerate(out) if e.event == "content_block_stop")
    assert all(e.event != "content_block_delta" or json.loads(e.data)["index"] != 0 for e in out[stop_at:])
    # Tool arguments pass through as-is (documented trade-off in the README).
    assert any("partial_json" in e.data for e in out)


# --- through the gateway, real sockets ----------------------------------------------

@pytest.fixture
def stack():
    with serving(mock_provider("primary", delay_ms=25)) as primary:
        config = Config(providers=[Provider("primary", primary + "/v1")], tenants={"tg-test": "acme"},
                        db_path=tempfile.mktemp(suffix=".sqlite3"))
        with serving(create_app(config)) as gateway:
            yield gateway


def ask(gateway, content, **extra):
    return {"url": gateway + "/v1/chat/completions", "headers": {"authorization": "Bearer tg-test"},
            "json": {"model": "m", "messages": [{"role": "user", "content": content}], "stream": True, **extra}}


def test_stream_is_redacted_end_to_end(stack):
    with httpx2.stream("POST", **ask(stack, "customer details please"), timeout=30) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        parser, events = Parser(), []
        for line in r.iter_lines():
            e = parser.feed(line)
            if e:
                events.append(e)
    text = text_of(events)
    for leak in ("priya.raman@example.com", "4111", "123-45-6789", "555-0132", "4532"):
        assert leak not in text
    assert text.startswith("Sure. The customer is Priya Raman, email [REDACTED], phone [REDACTED].")
    assert events[-1].comment.startswith(" tollgate redactions=")


def test_first_token_arrives_long_before_the_stream_ends(stack):
    # The mock takes ~25ms per tiny chunk, ~1.5s for the whole reply. A gateway
    # that accumulated the response would deliver its first byte at the end.
    started = time.monotonic()
    first = None
    with httpx2.stream("POST", **ask(stack, "customer details please"), timeout=30) as r:
        for line in r.iter_lines():
            if first is None and '"content": "S' in line:
                first = time.monotonic() - started
    total = time.monotonic() - started
    assert first is not None
    assert total > 1.0
    assert first < total * 0.2


def test_usage_chunk_only_when_requested(stack):
    def lines(**extra):
        with httpx2.stream("POST", **ask(stack, "hello", **extra), timeout=30) as r:
            return [line for line in r.iter_lines() if line.startswith("data: {")]
    assert not any('"usage"' in line for line in lines())
    assert any('"usage"' in line for line in lines(stream_options={"include_usage": True}))
