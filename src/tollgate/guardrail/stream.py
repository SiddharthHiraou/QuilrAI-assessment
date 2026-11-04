"""Redaction for provider event streams, OpenAI and Anthropic shapes.

Each codec takes one parsed SSE event in and returns the events to send on.
Text deltas go through a StreamRedactor per choice (OpenAI, n > 1) or per
content block (Anthropic), since interleaved choices are separate texts and
one redactor across them would join unrelated fragments.

Whatever a redactor is still holding when its choice or block finishes is
flushed into the stream before the event that finishes it, so no text is
lost and nothing arrives after the client thinks the message ended.
"""

import copy
import json

from tollgate.guardrail.redactor import StreamRedactor, redact
from tollgate.sse import Event


class _Codec:
    def __init__(self, labeled=False):
        self.labeled = labeled
        self.redactors = {}
        self.text_out = 0  # characters actually sent, for usage estimates

    def _redactor(self, key):
        if key not in self.redactors:
            self.redactors[key] = StreamRedactor(labeled=self.labeled)
        return self.redactors[key]

    @property
    def redactions(self):
        total = {}
        for r in self.redactors.values():
            for kind, n in r.found.items():
                total[kind] = total.get(kind, 0) + n
        return total

    @property
    def max_held(self):
        return max((r.max_held for r in self.redactors.values()), default=0)


class OpenAIChat(_Codec):
    """chat.completion.chunk events, terminated by data: [DONE]."""

    def __init__(self, labeled=False, keep_usage_chunk=True):
        super().__init__(labeled)
        self.keep_usage_chunk = keep_usage_chunk
        self.usage = None
        self._template = None
        self._finished = set()

    def on_event(self, event):
        if event.comment is not None:
            return [event]
        if event.data.strip() == "[DONE]":
            return self._flush_all() + [event]
        try:
            chunk = json.loads(event.data)
        except ValueError:
            return [event]  # not ours to judge; the client will see what the provider sent
        if not isinstance(chunk, dict):
            return [event]

        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices")
        if isinstance(choices, list) and not choices and self.usage is not None and not self.keep_usage_chunk:
            # The usage-only chunk we asked for on the client's behalf. Plenty of
            # client code does chunk.choices[0] and would crash on it.
            return []
        if not isinstance(choices, list):
            return [event]

        self._template = {k: chunk[k] for k in ("id", "object", "created", "model") if k in chunk}
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index", 0)
            delta = choice.get("delta")
            redactor = self._redactor(index)
            text = ""
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text = redactor.feed(delta["content"])
            if choice.get("finish_reason") is not None:
                text += redactor.flush()
                self._finished.add(index)
            if isinstance(delta, dict) and ("content" in delta or text):
                delta["content"] = text
                self.text_out += len(text)
        return [Event(data=json.dumps(chunk), event=event.event, id=event.id)]

    def _flush_all(self):
        # Provider ended without a finish_reason on some choice (or at all).
        out = []
        for index, redactor in self.redactors.items():
            if index in self._finished:
                continue
            text = redactor.flush()
            if text:
                self.text_out += len(text)
                chunk = {**(self._template or {}), "choices": [
                    {"index": index, "delta": {"content": text}, "finish_reason": None}]}
                out.append(Event(data=json.dumps(chunk)))
            self._finished.add(index)
        return out

    def on_end(self):
        return self._flush_all()


class AnthropicMessages(_Codec):
    """Messages API events: content_block_delta / content_block_stop / message_stop."""

    def on_event(self, event):
        if event.comment is not None or not event.data:
            return [event]
        try:
            payload = json.loads(event.data)
        except ValueError:
            return [event]
        kind = payload.get("type") if isinstance(payload, dict) else None

        if kind == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                delta["text"] = self._redactor(payload.get("index", 0)).feed(delta["text"])
                self.text_out += len(delta["text"])
                if not delta["text"]:
                    return []  # everything held back; an empty delta is just noise
                return [Event(data=json.dumps(payload), event=event.event)]
            # input_json_delta (tool arguments) and thinking deltas pass through
            # untouched. Thinking blocks are signed, editing one breaks it.
            return [event]

        if kind == "content_block_stop":
            index = payload.get("index", 0)
            tail = self.redactors[index].flush() if index in self.redactors else ""
            out = []
            if tail:
                self.text_out += len(tail)
                out.append(Event(event="content_block_delta", data=json.dumps(
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "text_delta", "text": tail}})))
            return out + [event]

        return [event]

    def on_end(self):
        return []


def redact_openai_completion(body, labeled=False):
    """stream: false goes through the same detectors, it's not a way around them."""
    body = copy.deepcopy(body)
    for choice in body.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = redact(message["content"], labeled)
    return body
