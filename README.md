# tollgate

An MCP server, an MCP security gateway, and an LLM gateway that redacts PII out of live
token streams and fails over between model providers. My submission for the QuilrAI AI
Field Developer / Solutions Engineer assessment.

The four tasks are separate modules with separate tests, but I built them to fit together
the way they would in a real deployment. The task 2 gateway proxies the actual task 1
server (over Streamable HTTP, using the official SDK on both sides) and runs task 3's
redactor over tool results. The LLM gateway is task 3's guardrail sitting on top of task
4's router.

```
                 +--------------------- task 2 ----------------------+
 MCP client  --> | MCP gateway :8800                                  | --> billing MCP server
 (Bearer JWT)    |  JWT auth, session binding, per-tool authz,        |     (task 1, real SDK,
                 |  header/body checks, tool pinning, PII on results  |      stdio or HTTP)
                 +----------------------------------------------------+
                                             | same redactor
                 +------------------- tasks 3 + 4 --------------------+
 app         --> | LLM gateway :8830                                  | --> primary provider
 (API key)       |  token budget (SQLite) -> router -> stream         | --> secondary on 429,
                 |  redaction on every delta, as it arrives           |     3000ms, 5xx
                 +----------------------------------------------------+
```

## Running it

```bash
make install     # venv + deps (Python 3.11 or newer)
make test        # 286 tests, about 40s, no network, no API keys
make demo        # every task end to end, narrated, about 12s
```

If you only have two minutes, run `make demo`. It drives the real services with the
official MCP client and prints what happens, including the things that don't (like the
downstream call a refused `admin_` tool never makes).

The whole stack also runs in containers. Only the two gateways publish ports; the MCP
server and the model providers are on an internal network, so the gateways are the only
way in.

```bash
docker compose up --build
TOKEN=$(.venv/bin/tollgate token --role viewer)    # dev JWT for the MCP gateway
```

Other targets: `make bench` (what the guardrail costs, over real sockets), `make inspect`
(task 1 in the MCP Inspector), `make help`.

## Where each requirement lives

Every scoring criterion from the brief, the code that handles it, and the test that
shows it.

**Task 1: MCP server with strict validation** ([`src/tollgate/mcp_server/`](src/tollgate/mcp_server/))

| Criterion | Code | Shown by |
|---|---|---|
| stdout is pure JSON-RPC | SDK claims fd 1; [`stdio.py`](src/tollgate/mcp_server/stdio.py) covers the `sys.stdout` object | `test_every_stdout_line_is_jsonrpc`, run with chaos mode on |
| JSON-RPC error mapping | -32602 / -32601 / -32700 / -32600 / -32603, business errors as `isError` | `test_error_codes_on_the_wire`, `test_unparseable_lines_get_a_parse_error` |
| Schema validation, edge cases | Pydantic strict, `extra="forbid"`, in [`models.py`](src/tollgate/mcp_server/models.py) | 59 cases in `test_mcp_server_tools.py` |

**Task 2: MCP security gateway** ([`src/tollgate/mcp_gateway/`](src/tollgate/mcp_gateway/))

| Criterion | Code | Shown by |
|---|---|---|
| Bearer token to role | JWT, pinned HS256, `exp` required, [`auth.py`](src/tollgate/mcp_gateway/auth.py) | 11 forged-token cases in `test_bad_credentials_are_401` |
| `tools/list` forwarded transparently | byte-for-byte, both protocol eras | `test_tools_list_is_forwarded_transparently` (compared against a direct call) |
| `admin_*` needs admin, else -32001, downstream not called | [`policy.py`](src/tollgate/mcp_gateway/policy.py), `Gateway.check` | `test_viewer_is_refused_admin_tools_and_downstream_never_sees_it` |
| JSON-RPC wire parsing | ids (including `0`), notifications, batches, envelopes, [`jsonrpc.py`](src/tollgate/mcp_gateway/jsonrpc.py) | `test_malformed_envelopes`, `test_batch_is_authorised_per_element` |
| Proxying | Streamable HTTP POST/GET/DELETE, SSE relayed event by event, [`app.py`](src/tollgate/mcp_gateway/app.py) | official SDK client through the gateway, `auto` and `legacy` modes |

**Task 3: streaming PII redaction** ([`src/tollgate/guardrail/`](src/tollgate/guardrail/))

| Criterion | Code | Shown by |
|---|---|---|
| Chunking and buffer state | [`redactor.py`](src/tollgate/guardrail/redactor.py), holds back only text that can still change | `test_property_stream_equals_one_shot` (Hypothesis, 600 cases) |
| Matching across partial streams | family-run settle point, [`patterns.py`](src/tollgate/guardrail/patterns.py) | `test_pii_split_at_every_possible_point` |
| Memory | hard cap of 346 chars held, whatever the input | `test_holdback_is_capped_on_adversarial_runs` |
| Low TTFT | nothing waits for a sentence or a chunk count | `test_first_token_arrives_long_before_the_stream_ends`, `make bench` |

**Task 4: rate limiting and failover** ([`src/tollgate/router/`](src/tollgate/router/))

| Criterion | Code | Shown by |
|---|---|---|
| Token-aware sliding window, SQLite on disk | reserve/settle/release in `BEGIN IMMEDIATE`, [`limiter.py`](src/tollgate/router/limiter.py) | `test_window_slides_instead_of_resetting`, `test_two_processes_sharing_one_file` |
| Eviction and token tracking | expired rows deleted on every reserve, settled to real usage | `test_expired_rows_are_evicted`, `test_reservations_settle_to_real_usage` |
| 429 / 3000ms failover, timeout races | one `asyncio.timeout` owns the deadline, [`router.py`](src/tollgate/router/router.py) | `test_default_deadline_is_3000ms`, `test_timeout_fails_over_..._cancels_the_primary` |
| Standardised, sanitised errors | closed vocabulary, [`errors.py`](src/tollgate/router/errors.py) | `test_all_providers_failing_is_sanitised` (greps for 7 planted secrets) |

## Task 1: MCP server

```bash
make run-mcp       # stdio
make inspect       # or poke at it in the MCP Inspector
```

To use it from Claude Desktop or any other stdio MCP client, see
[`examples/claude_desktop_config.json`](examples/claude_desktop_config.json).

**Why the low-level `Server` and not `MCPServer`.** The SDK's high-level `MCPServer` turns
argument validation failures into `isError` tool results, which is what the spec
recommends for tool errors (the model can read them and retry). The brief asks for invalid
input to be rejected with JSON-RPC error codes, and that needs control over what gets
raised. So validation goes through the low-level server and comes back as `-32602` with a
list of violations.

**Errors split three ways.**

| What went wrong | On the wire |
|---|---|
| Bad arguments, unknown tool | `-32602`, with `{field, issue, type}` per violation. The value is never echoed back; that's how a card number pasted into the wrong field ends up in someone's log aggregator. |
| Valid request, business said no (frozen account, balance too low, unknown customer) | a normal result with `isError: true`. "The account is frozen" is useful to the model, and a protocol error would hide it. |
| Something crashed | `-32603 Internal error` with a `call_id`. Everything else goes to the stderr log under that id. |

The SDK doesn't answer lines that aren't JSON-RPC at all: its stdin reader turns them into
an exception and the serve loop drops it, so a client that sent a truncated frame waits
forever. [`AnswerUnparseable`](src/tollgate/mcp_server/stdio.py) wraps the read stream and
replies `-32700` (bad JSON) or `-32600` (wrong shape, with the id when one can be recovered).

**Keeping stdout clean.** `stdio_server()` already dups fd 1 to a private descriptor and
points fd 1 at stderr, which covers `os.write(1, ...)` and child processes. On top of that
`sys.stdout` is pointed at stderr for code holding the object, and for the SDK's fallback
path where the fd juggling fails. That swap has to happen inside `stdio_server()`, not
before it: the SDK checks that `sys.stdout` is backed by fd 1 before claiming it, and if
you swap first it quietly serves the wire from stderr. The server logs "ready" and every
client then hangs. Instead of claiming this works, `TOLLGATE_CHAOS_STDOUT=1` makes every
handler write to stdout with `print()`, `os.write(1)` and a subprocess, and the stdio tests
run with it on.

**Validation details that bit, or would have.**

- `[0-9]` rather than `\d`. Python's `\d` matches Arabic-Indic and fullwidth digits, so
  `CUST-١٠٠٠١` would pass the pattern and then miss every lookup.
- `"CUST-10001\n"` is rejected. Python's `$` matches before a trailing newline; pydantic's
  Rust regex engine doesn't, and there's a test pinning that down.
- Strict types: `"50"` isn't a number and `true` isn't `1`. NaN, infinity and sub-cent
  amounts are refused, and so is anything over the $10,000 cap.
- `reason` is stripped before the length check (ten spaces isn't a reason) and can't
  contain control, zero-width or bidi characters, which can make an audit log line say
  something other than what it stores.
- Unknown fields are an error, so `resaon` doesn't silently drop the audit trail.
- Refunds take an optional `idempotency_key`, and check-then-debit holds a lock:
  20 concurrent $100 refunds against $1,200 gives exactly 12 successes.

## Task 2: MCP security gateway

```bash
make run-gateway   # billing server on :8801, gateway on :8800
```

**It's a real Streamable HTTP proxy.** Sessions, SSE replies, the GET stream and DELETE all
pass through, as do the 2026-07-28 stateless requests. SSE replies are relayed event by
event, so a long-running tool's progress notifications reach the client as they happen
(there's a timing test for that). The downstream in every gateway test is the SDK's own
server, and the protocol-level tests use the SDK's own client, so nothing here is checked
against a mock I wrote myself.

**Authentication failures are HTTP 401, authorization failures are JSON-RPC `-32001`.**
401 with `WWW-Authenticate` is what MCP clients and OAuth libraries already know how to
handle. A refused tool goes back as a JSON-RPC error in a 200, not as a 403: MCP clients
read 403 with `insufficient_scope` as a cue to start step-up authorization, and that's not
what's happening here.

**Deny closed.** A refused call is answered by the gateway and never forwarded. The tests
check the downstream's own record of what it received, because a proxy that forwards
`admin_reset_key` and then hides the response has still reset the key. Beyond that:

- A `tools/call` with no usable `params.name` is `-32602` and isn't forwarded. "No name,
  so no `admin_` prefix, so allow it" is exactly the bypass to design against.
- Tool names outside `[A-Za-z0-9_.-/]{1,128}` are refused before policy runs, so
  `ａｄｍｉｎ_reset_key` and zero-width tricks never reach the matcher. Matching is
  case-insensitive, so `ADMIN_reset_key` doesn't slip through either.
- A `tools/call` sent as a notification is dropped. It isn't valid MCP, but "it has no id
  so the server won't run it" is a guess about the downstream.
- JSON-RPC batches were removed from MCP in 2025-06-18 and the SDK server now rejects them.
  The gateway decides each element separately, so a batch can't smuggle an admin call
  past the check, then sends the allowed ones downstream one at a time and puts the
  replies back in order.

**Headers can't disagree with the body.** 2026-07-28 clients copy the method and tool name
into `Mcp-Method` and `Mcp-Name` so that load balancers and WAFs can route without parsing
JSON. That creates a new bypass: headers saying `get_customer_record` on a body that calls
`admin_reset_key` sails through anything that filters on headers. The gateway decides on
the body, which is what actually runs, and refuses any mismatch with `-32020`
(base64-encoded header values are decoded before comparing).

**Session ids are bound to the caller.** As far as the downstream is concerned an
`Mcp-Session-Id` is a capability: present it, get the session. The gateway ties each id
to the subject that created it, so a viewer replaying an admin's session id gets the same
404 an unknown session gets.

**The caller's credential stops here.** The downstream gets the gateway's own token (if
configured) and `X-Tollgate-Subject` / `X-Tollgate-Role`. Client-supplied `X-Tollgate-*`
headers, cookies and the client's token are stripped. Forwarding the caller's token would
make this a confused deputy and put tokens in downstream logs.

**Tool pinning** ([`pins.py`](src/tollgate/mcp_gateway/pins.py)). Tool descriptions are
text the model follows, so they're an injection surface. The first definition seen for
each tool is pinned by hash. A later change (the "rug pull": approved as harmless, edited
afterwards) or invisible characters in the description get flagged. In `warn` mode
(default) they're logged and pass through untouched, since the brief says forward
`tools/list` transparently. In `enforce` mode the tool disappears from `tools/list` and
calls to it get `-32001` until an admin resets the pins.

**PII redaction on tool results** ([`dlp.py`](src/tollgate/mcp_gateway/dlp.py)). The gateway
sees every tool result before the agent does, which makes it the right place to keep a
customer's email and phone out of the model's context. By default viewers get redacted
results and admins see raw data. Counts (never values) go in `_meta`.

Smaller things: `-32002` is skipped for the gateway's own codes because the 2026-07-28
allocation policy lists it as retired and never to be reused; downstream failures are a `502` with `-32003` and no
address; Origin is checked against the parsed hostname (DNS rebinding);
`TOLLGATE_FILTER_TOOLS_LIST=1` hides tools a role can't call.

Policy lives in code by default and can be loaded from TOML
([`examples/policy.toml`](examples/policy.toml)). Rules are ordered, first match wins, and
an unknown role matches nothing.

## Task 3: streaming PII redaction

```bash
make run-llm       # mock providers on :8831/:8832, gateway on :8830
make bench
```

A provider streams `"my card is 4111"`, `" 1111 111"`, `"1 1111, ok?"`. A regex per chunk
misses it, because the card isn't in any single chunk. Buffering the whole response
catches it but throws away streaming: TTFT becomes total generation time and memory grows
with the response.

**What I do instead is find the point before which nothing can change.** Every detector is
built from one of two character families: emails and API keys only from `WORD` characters,
cards, SSNs and phone numbers only from `NUMERIC` ones. A regex attempt starting at
position *p* reads only characters of its family plus one character of lookahead, so its
result can only change when more text arrives if every character from *p* to the end of the
buffer is in that family. Walk back from the end over each family's trailing run; the
earlier of the two is the settle point. Everything before it is redacted and sent, and only
the tail is kept.

In prose that tail is usually just the last partial word, since a space ends the `WORD` run
and a letter ends the `NUMERIC` one. Unbroken runs (base64, a wall of digits) are capped at
`MAX_SPAN` = 346 characters, because every quantifier is bounded and no attempt can read
further than that. Memory is O(longest possible match) and never depends on response length.

That argument is easy to get subtly wrong, so it's tested as a property: for any text and
any way of splitting it, streamed output must be byte-identical to redacting the whole text
in one go. Hypothesis runs 600 generated cases built from PII fragments and separators, plus
arbitrary Unicode, and there's a test that splits a card at every possible position.

| Measured (`make bench`) | |
|---|---|
| TTFT, gateway with redaction vs provider direct | +3.8 ms (1.3 ms of that is the proxy hop) |
| Redactor throughput, 4-char chunks | ~580k chunks/s on one core |
| Held back in prose | 1 character (the end of the current word) |

The extra couple of milliseconds is the redactor holding the first partial word until it
knows it isn't the start of an email address. That's the price of not leaking.

**False positives matter as much as misses.** A guardrail that mangles good output gets
switched off by the first annoyed team. Cards need a valid Luhn checksum and a real issuer
prefix, SSNs skip ranges the SSA never issues, and phone numbers need their separators. So
`order 1234567890123456`, `invoice 999-88-7777`, dates, versions and IP addresses all pass
through untouched; there are 15 cases like that.

**Streaming details.**

- One redactor per choice (OpenAI `n > 1`) or content block (Anthropic). One shared buffer
  would glue interleaved choices together.
- Whatever is held back when a choice finishes is flushed before the event that carries
  `finish_reason`, so nothing arrives after the client thinks the message ended.
- The router asks the provider for usage on the final chunk so the limiter can settle on
  real numbers, and the gateway strips that chunk again if the client didn't ask for it.
  Plenty of client code does `chunk.choices[0]` and would crash on it.
- Stats go out as an SSE comment (`: tollgate redactions=...`), which every client parser
  ignores, rather than as a custom event some SDKs choke on.
- `X-Accel-Buffering: no`, because nginx in front of a streaming endpoint buffers by default
  and quietly turns the stream into one response at the end.
- `stream: false` goes through the same detectors. It isn't a way around them.

**What it doesn't catch.** Names and street addresses: `Priya Raman` passes through, as the
demo shows. Regex can't find those without unacceptable false positives; in production I'd
add an NER pass on a small model for those classes and accept its latency. Tool-call
argument deltas and Anthropic thinking blocks pass through untouched (redacting arguments
can break legitimate calls, and thinking blocks are signed).

## Task 4: rate limiting and failover

**Reserve, then settle.** Counting tokens after a call returns lets N concurrent requests
all read the same "under the limit" total and all go through. Agent traffic makes that the
common case: one turn fans out ten parallel calls. So:

1. `reserve` runs inside one `BEGIN IMMEDIATE` transaction: delete expired rows, sum the
   window, then either refuse or insert a row for the worst case (prompt estimate plus
   `max_tokens`; if the client didn't set `max_tokens` the gateway sets 1024 so there is
   a worst case).
2. `settle` corrects the row with the provider's reported usage. Estimates are always wrong;
   this way the error lasts one request, not one window.
3. `release` deletes the row when every provider failed. An outage shouldn't also cost the
   tenant their minute.

200 concurrent reservations against room for 50 get exactly 50, and two limiter instances
on one file (standing in for two gateway workers) can't oversell either. It's a true sliding
window, since one-minute buckets let a tenant spend the limit twice across a boundary.
`Retry-After` is computed by walking the oldest rows to when enough capacity actually frees
up; a flat 60 makes every client wait a full window and then retry in the same second.

Keys are stored as SHA-256 hashes (there's a test that greps the database file), the
window survives a restart, and WAL plus `busy_timeout` keeps multiple processes on one file
from failing with "database is locked". A request bigger than the whole budget is a 400,
not a 429, because a `Retry-After` would be a lie.

**Failover policy.**

| Primary returned | Fail over? | Why |
|---|---|---|
| 429 | yes | capacity, and a `Retry-After` opens the breaker for exactly that long |
| no response within 3000ms | yes | |
| 5xx, connection error, unparseable 200 | yes | |
| 401, 403, 404 | yes, and logged as an error | our key or model name for that provider is wrong; not the caller's fault |
| 400, 413, 422 | no | the request is wrong, and the backup would make the caller wait twice to say so |

One `asyncio.timeout` owns the deadline and httpx's read timeout is off, so there's one
authority and no race between two timers naming the same failure differently. Cancellation
reaches the transport, and a test asserts the primary's request was actually torn down
rather than left running. For streams the deadline covers time to the first event; once a
token has gone to the client, failing over would send it text that contradicts what it
already has, so a later failure ends the stream with an error event instead.

A circuit breaker skips a primary that keeps failing (5 in a row opens it for 15s, then one
probe), because paying the full 3000ms on every request during an outage turns a provider
outage into a latency outage. Skipped providers show up as `circuit_open` in the attempts
list.

**Errors.** One closed vocabulary with fixed messages:

```
upstream said : Traceback ... upstream=10.12.4.7:9000 api_key=sk-live-SECRET... org_id=org-7f3a2b9c
client sees   : {"error": {"type": "providers_unavailable",
                 "message": "No model provider could serve this request.",
                 "request_id": "req_f36996cd8b4f4153",
                 "attempts": [{"provider": "primary", "outcome": "server_error"}, ...]}}
```

The upstream body goes to the stderr log under the same `request_id`. 404s, 405s and
unhandled exceptions use the same shape, so a client only ever has to parse one thing.

**On SQLite.** It's right for this: on disk, transactional across processes on one host,
nothing to run. The limit is that one host. Behind a load balancer each instance enforces
its own window, so multi-node wants Redis running the same reserve/settle protocol (a Lua
script for the reserve). The limiter's interface doesn't assume SQLite.

## Things I found reading the 2026-07-28 spec and SDK 2.3

These shaped the design, and I didn't know most of them before starting:

- 2026-07-28 requests are stateless POSTs with no handshake and no session, and mirror the
  method and name into headers. A gateway now has to handle both eras on one endpoint.
- `-32002` and `-32042` are retired and must never be reused, and `-32020..-32099` are
  reserved for the spec. `-32002` is the obvious next code after the brief's `-32001`, so
  it's an easy one to pick by accident.
- The SDK silently drops unparseable stdio lines (fixed here), and answers a JSON-RPC batch
  with a 400 that includes a full pydantic validation dump.
- `MCPServer` reports validation failures as `isError` results, the low-level `Server`
  lets you choose.

## Using it with real things

- **MCP Inspector:** `make inspect`.
- **Claude Desktop / any stdio client:** [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json).
- **Remote MCP clients:** point them at `http://localhost:8800/mcp` with
  `Authorization: Bearer $(tollgate token --role viewer)`.
- **Real model providers:** set `TOLLGATE_PRIMARY_BASE_URL=https://api.openai.com/v1`,
  `TOLLGATE_PRIMARY_API_KEY`, `TOLLGATE_PRIMARY_MODEL`, and the same for `SECONDARY`. Any
  OpenAI-compatible endpoint works, including Anthropic's. Everything is listed in
  [`.env.example`](.env.example).

## What I'd change for production

- **Identity:** verify JWTs against an IdP's JWKS instead of a shared secret, and publish
  OAuth protected-resource metadata so MCP clients can discover how to log in. Only
  `auth.py` changes.
- **Policy:** per-argument rules, not just per-tool. `trigger_refund` over some amount
  should need a second approval, and the gateway can't express that today. I'd move policy
  to OPA or signed config so it changes without a deploy.
- **Pins and sessions** live in memory per gateway process. Multi-instance needs them in a
  shared store, and pins should be signed and reviewable.
- **Detection:** NER for names and addresses; the provider's own tokenizer instead of a
  chars-per-token estimate.
- **Limits:** Redis for multi-node windows, per-model and per-tenant limits, and hedged
  requests for latency-sensitive traffic.
- **Everywhere:** mTLS between components, OpenTelemetry spans per tool call and per
  provider attempt, and a hashed lockfile.

[`docs/zero-trust-runbook.md`](docs/zero-trust-runbook.md) covers the overview's
"troubleshooting zero-trust network deployments" area: what breaks when these services sit
behind mTLS, egress proxies and TLS inspection, organised by the symptom you'd get paged
for. [`NOTES.md`](NOTES.md) is my working log, including the approaches that didn't work.

## Layout

```
src/tollgate/
  mcp_server/     task 1   models.py, store.py, server.py, stdio.py
  mcp_gateway/    task 2   auth.py, policy.py, jsonrpc.py, sessions.py, pins.py, dlp.py, app.py
  guardrail/      task 3   patterns.py, redactor.py, stream.py
  router/         task 4   limiter.py, router.py, tokens.py, errors.py
  llm_gateway/    tasks 3 + 4 wired into one OpenAI-compatible endpoint
  mock_llm/       fake provider with failure modes: rate_limit, error, slow, stall, reject
  sse.py, log.py  shared; every service logs JSON lines to stderr, never stdout
tests/            286 tests, mostly over real sockets
scripts/          demo.py, bench.py
```
