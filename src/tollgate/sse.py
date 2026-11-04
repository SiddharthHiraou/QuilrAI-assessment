"""Server-sent events, both directions.

Used by the MCP gateway (Streamable HTTP answers POSTs with SSE) and by the LLM
gateway (provider token streams). Follows the WHATWG parsing rules closely
enough for both: multi-line data, comments, id and retry fields.
"""

from dataclasses import dataclass


@dataclass
class Event:
    data: str = ""
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    # A comment-only block (": ping"). Proxies use these as keepalives, so they
    # get forwarded instead of dropped.
    comment: str | None = None

    def encode(self):
        lines = []
        if self.comment is not None:
            lines.append(":" + self.comment)
        if self.event is not None:
            lines.append("event: " + self.event)
        if self.id is not None:
            lines.append("id: " + self.id)
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        if self.comment is None or self.data:
            lines.extend("data: " + part for part in self.data.split("\n"))
        return ("\n".join(lines) + "\n\n").encode()


class Parser:
    """Feed it lines, get events back when a blank line closes one."""

    def __init__(self):
        self._reset()

    def _reset(self):
        self._data = []
        self._event = None
        self._id = None
        self._retry = None
        self._comment = None

    def feed(self, line):
        line = line.rstrip("\r\n")
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            self._comment = line[1:]
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            if "\0" not in value:
                self._id = value
        elif field == "retry":
            if value.isdigit():
                self._retry = int(value)
        return None

    def flush(self):
        """End of stream. A final event without its blank line still counts."""
        return self._dispatch()

    def _dispatch(self):
        if not self._data and self._event is None and self._comment is None and self._id is None:
            return None
        event = Event(
            data="\n".join(self._data),
            event=self._event,
            id=self._id,
            retry=self._retry,
            comment=self._comment if not self._data else None,
        )
        self._reset()
        return event


async def aiter_events(lines):
    parser = Parser()
    async for line in lines:
        event = parser.feed(line)
        if event is not None:
            yield event
    event = parser.flush()
    if event is not None:
        yield event
