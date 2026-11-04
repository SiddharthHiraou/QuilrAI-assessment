# Troubleshooting tollgate in a zero-trust deployment

The assessment overview lists "troubleshooting zero-trust network deployments" as a focus
area without a task attached, so this is the runbook I'd want if these services were
deployed at a customer: mTLS between workloads, an identity-aware proxy or WAF at the edge,
a mandatory egress proxy (often doing TLS inspection), and default-deny network policy.

It's organised by the symptom someone reports, because that's how the ticket arrives.

## Before anything else: find the hop

Every service logs JSON lines to stderr with a `request_id`, and the gateways return it in
`x-request-id`. Get it from the client and follow it:

```bash
docker compose logs mcp-gateway llm-gateway | grep "$REQUEST_ID" | jq .
```

If the gateway never logged the id, the request died in front of it (edge proxy, WAF, DNS,
mTLS). If it logged a decision but the downstream never did, it's the gateway-to-downstream
hop. That one split decides which half of this page applies.

```bash
# DNS and TLS to the gateway, from where the client sits
getent hosts mcp.internal
openssl s_client -connect mcp.internal:443 -servername mcp.internal </dev/null 2>&1 | head -20

# is our workload certificate valid right now, and for which identity?
openssl x509 -in /var/run/secrets/workload/cert.pem -noout -dates -subject -ext subjectAltName
```

---

## "The MCP client connects and then hangs forever"

**stdio server, logs say ready.** Something put non-JSON on stdout and the client's frame
parser is now waiting for a line that will never parse. In containers it's usually a shell
wrapper printing a banner, or an entrypoint script that echoes. Check the raw wire:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | python -m tollgate.mcp_server 2>/dev/null | head -3
```

Every line must be JSON. The server itself is covered (fd and object level, see
`mcp_server/stdio.py`), but a wrapper script that runs before Python isn't.

**HTTP, through the gateway.** If `initialize` works but the first tool call hangs, look for
an edge proxy buffering `text/event-stream`. Legacy-era MCP answers POSTs with SSE, and a
buffering proxy holds the reply until the stream closes. Same fix as the next section.

## "Streaming works locally but arrives all at once in staging"

TTFT jumps from milliseconds to the full generation time. Something between the client and
the gateway is buffering:

- **nginx / ingress-nginx**: `proxy_buffering` is on by default. The gateways send
  `X-Accel-Buffering: no`, which nginx honours, unless `proxy_ignore_headers` is set to
  include it. Otherwise set `proxy_buffering off` on the location.
- **Compression**: gzip on `text/event-stream` buffers until a block fills. Exclude that
  content type at the proxy.
- **TLS-inspecting egress or corporate proxies**: many buffer whole responses to scan them.
  Ask for an inspection bypass for the gateway's hostname, or for the provider's hostname on
  the egress side.

Check from inside the network, one hop at a time:

```bash
curl -sN -o /dev/null -w 'ttfb %{time_starttransfer}s total %{time_total}s\n' \
  http://llm-gateway:8830/v1/chat/completions -H 'authorization: Bearer tg-dev-acme' \
  -d '{"model":"m","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

ttfb close to total means buffering at or before the hop you're measuring.

## "Long tool calls or the MCP event stream die at exactly 60 seconds"

An idle timeout somewhere: AWS ALB defaults to 60s, nginx `proxy_read_timeout` defaults to
60s, and some service meshes cut idle HTTP/1.1 streams too. Long-lived SSE (the legacy GET
stream, a slow tool call) needs the timeout raised on every hop, or keepalive traffic. The
SDK server sends SSE pings for long silent handlers on the 2026-07-28 path; for the legacy
GET stream, raise the timeout.

On the LLM side the gateway has its own idle limit (`TOLLGATE_STREAM_IDLE_S`, default 30s)
and ends the stream with a `stream_interrupted` error event rather than hanging. If clients
see that event, the provider went quiet, not the network.

## "401 for everyone, right after a deploy"

In order of how often I'd expect each:

1. **Clock skew.** JWT `exp`/`iat` checks with a node whose clock drifted. The gateway allows
   30s leeway; beyond that, fix NTP on the node. `date -u` on the gateway pod against the
   IdP's clock.
2. **Secret or key mismatch across replicas.** One replica got the new
   `TOLLGATE_JWT_SECRET` and the others didn't, so roughly 1 in N requests succeeds. The
   `auth failed` log line names the reason without the token.
3. **Wrong audience or issuer.** Tokens minted for another service. The gateway requires
   `aud=tollgate-mcp` and `iss=tollgate`.
4. **An edge proxy stripping `Authorization`.** Some identity-aware proxies consume it and
   pass their own header instead. The gateway log will show `missing bearer token`.

## "Viewers can call admin tools" / "admins get -32001"

Look at the decision line, it has the rule that matched:

```bash
docker compose logs mcp-gateway | jq 'select(.decision) | {subject, role, tool, decision, rule}'
```

If `role` isn't what you expect, it's the token: decode it (`echo $TOKEN | cut -d. -f2 |
base64 -d`) and check the `role` claim. If the rule is wrong, it's `TOLLGATE_POLICY`. If an
admin tool reached the downstream without a decision line at all, something is talking to
the downstream directly, which network policy should make impossible: the billing server
belongs on a network only the gateway can reach (that's how `docker-compose.yml` is set up).

## "-32020 header mismatch on every call"

Modern-era (2026-07-28) clients send `Mcp-Method` and `Mcp-Name` headers, and both the
gateway and the SDK server refuse a request whose headers disagree with its body. If every
call fails this way, something in the path is rewriting or dropping those headers. WAFs and
API gateways with header allowlists are the usual cause. Allow `Mcp-*` through.

## "Session not found (404) at random"

Legacy-era sessions live in one downstream process (and the gateway's session binding in
one gateway process). Behind a load balancer without affinity, a client's second request
lands on a replica that has never seen its session. Either pin sessions (affinity on
`Mcp-Session-Id`) or move clients to 2026-07-28, which is stateless and doesn't care which
replica answers.

It's also what a caller sees when it presents someone else's session id. If the logs show
`session belongs to another subject`, that's the binding doing its job, not a bug.

## "Downstream says Invalid Host header / 421"

The SDK's Streamable HTTP server enables DNS rebinding protection when bound to localhost,
and rejects requests whose `Host` isn't localhost. That breaks the moment the gateway reaches
it by service name. Bind it to the service interface (`--host 0.0.0.0`, as compose does) and
keep it on a private network instead.

## "Every provider attempt fails with connection_error"

The router logs each attempt. If both providers fail with `connection_error` it's almost
never the providers:

- **Egress allowlist** missing `api.openai.com` (or whichever provider). Test from the pod:
  `curl -sv https://api.openai.com/v1/models -o /dev/null`.
- **Proxy variables**: httpx honours `HTTPS_PROXY` / `NO_PROXY`. A missing `NO_PROXY` for
  internal hosts sends the mock or internal providers to the egress proxy.
- **TLS inspection**: `CERTIFICATE_VERIFY_FAILED` in the log detail means the egress proxy
  re-signs traffic with a corporate CA the container doesn't trust. Mount the CA and set
  `SSL_CERT_FILE`, or install it into the image's trust store.

If only the primary fails and the secondary works, failover is doing its job; check
`/healthz` for the breaker state.

## "Tenants are getting more than 50k tokens a minute"

The limiter's window lives in one SQLite file. Two replicas with their own files each
enforce their own 50k, so a tenant spread across N replicas gets N times the limit. On one
host, point every worker at the same file (WAL and `busy_timeout` make that safe). Across
hosts, it needs a shared store; see "On SQLite" in the README.

## "Rate limited even though usage is low"

Reservations are made for the worst case (prompt estimate plus `max_tokens`) and settled
down to real usage afterwards. A client sending `max_tokens: 16000` on every request books
16k per request until each one finishes. Check `/v1/usage` while the requests are in
flight, and set `max_tokens` to what the client actually needs.
