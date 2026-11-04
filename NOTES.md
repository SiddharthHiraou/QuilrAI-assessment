# Working notes

Kept while building. The README has the conclusions; this has how I got there, including
the parts that didn't work.

## Testing against the real thing

The easy version of task 2 is a proxy that speaks plain JSON-RPC to a mock you wrote
yourself. It passes its own tests and breaks the moment it's put in front of a real MCP
server, because Streamable HTTP answers with SSE and needs `Mcp-Session-Id` handled. So the
downstream in every gateway test is the SDK's own server, and the client is the SDK's own
client.

## The SDK moved a lot

mcp 2.3 speaks two protocol eras. I only found that out by running the official client
against the official server with request logging on, which I'd recommend to anyone
building a proxy:

- `mode="auto"` sends `server/discover` first, then stateless POSTs with
  `Mcp-Method` / `Mcp-Name` headers and JSON replies. No session.
- `mode="legacy"` does `initialize`, gets a session id, opens a GET stream, and every POST
  reply is SSE.

That's why the gateway handles both, and the routing headers are where the header/body
mismatch check came from. If anything in front of the gateway routes on headers, the
body can say something else.

## Task 1: the SDK drops bad lines

Sent a truncated JSON line on stdin while poking at error codes. No reply at all, the
client would just wait. The stdin reader turns it into an exception on the read stream and
the serve loop ignores it. Wrapped the read stream (`AnswerUnparseable`) to answer -32700 /
-32600. Had to keep `last_context` on the wrapper, because the serve loop restores
contextvars from it.

Also: `$` in Python's `re` matches before a trailing newline, so `^CUST-\d{5}$` accepts
`"CUST-10001\n"`. Pydantic uses the Rust regex engine for `pattern`, which doesn't, but I
switched `\d` to `[0-9]` anyway after realising `\d` matches Arabic-Indic digits. Both have
tests now.

## Task 3: the cut point, third attempt

1. Hold back a fixed tail (64 chars). Correct, but it delays every response by 64
   characters for no reason.
2. Hold back the trailing run of "characters that could be part of PII", one combined set.
   Broken: with space in the set (needed for `4111 1111 ...`) every sentence is one run, so
   it buffers everything. Without space, cards get split.
3. Two families instead of one set. Emails and keys are built from `WORD` characters, cards,
   SSNs and phones from `NUMERIC` ones, and a match never crosses a character outside its
   own family. Walk back over each family separately and take the earlier start. Prose
   settles at the last space; digit groups stay together.

The case in 3 I nearly got wrong: if the `NUMERIC` run starts in the middle of a `WORD` match (say
`sk-...0123 4111`, where the run starts at `0123`), cutting there splits the key: the first
half gets redacted on its own and `0123` leaks. The fix is in how matches near the cut are
handled. A match that *starts* before the settle point is final even if it runs past it,
because its own run already ended inside the buffer. So it gets emitted whole. I caught it
working through why the settle point is safe, before running anything, then wrote a fuzz
loop to make sure. The Hypothesis property would have caught it too.

Second thing that took a while: the hard cap. A run of 20,000 `7`s never ends a family run,
so without a cap the buffer grows forever. Every quantifier in `patterns.py` is bounded, so
no regex attempt reads more than `MAX_SPAN` characters, and anything further back than that
is settled regardless of families.

Considered the third-party `regex` module's partial matching, which does this "properly".
Skipped it: another dependency, and the family argument is something a reviewer can check by
reading, which partial matching isn't.

## Task 4

Started the limiter tests with the window boundary off by one: I expected a row at t=1059 to
still count at t=1119. It doesn't, the window is (now - 60, now]. Fixed the test, not the
code, and left a comment on the boundary line so it's obvious next time.

The rate limit test in `test_llm_gateway.py` also failed the first time for a better reason:
reservations settle down to real usage after each request, so sequential small requests
never hit the limit. That's the design working. Rewrote the test with long prompts so the
real usage adds up.

Timeouts: `asyncio.timeout` around the whole attempt, httpx read timeout off. Two timers on
one deadline means sometimes it's a `ReadTimeout` and sometimes a `TimeoutError` for the
same event, and the failover logic would have to treat both.

## Testing

Switched from pytest-asyncio to anyio's plugin after the first fixture that opened an SDK
client: the SDK's
`Client` opens an anyio task group, pytest-asyncio runs async fixtures in a different task
from the test, and anyio refuses to exit a cancel scope from another task.

Anything about streaming or timing runs over real sockets (`tollgate.testing.serving`).
httpx's `ASGITransport` buffers the whole response before handing it over, so it would
happily pass a gateway that accumulates the stream.

## Left out

- OAuth discovery (protected resource metadata). The gateway does the resource-server half
  (401 plus `WWW-Authenticate`); there's no authorization server in this setup to point at.
- Persisting pins and session bindings. In memory per process, see the README.
- Server-initiated requests on the GET stream (sampling, elicitation) pass straight through
  with no policy. Worth gating in a real deployment.
