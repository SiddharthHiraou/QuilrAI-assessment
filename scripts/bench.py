"""make bench: what the guardrail costs.

Two numbers matter for a streaming guardrail. Added time to first token, over
real sockets, compared against hitting the provider directly and against the
gateway with redaction switched off. And raw redactor throughput, which says
whether it could ever be the bottleneck.
"""

import json
import logging
import statistics
import tempfile
import time

import httpx2

from tollgate.guardrail.redactor import StreamRedactor
from tollgate.llm_gateway.app import create_app
from tollgate.llm_gateway.config import Config
from tollgate.mock_llm.app import PII_REPLY
from tollgate.mock_llm.app import create_app as mock_provider
from tollgate.router.router import Provider
from tollgate.testing import serving

logging.getLogger("tollgate").setLevel(logging.CRITICAL)

RUNS = 30
BODY = {"model": "m", "messages": [{"role": "user", "content": "customer details please"}], "stream": True}


def measure(url, headers):
    ttft, total = [], []
    with httpx2.Client(timeout=30) as client:
        for _ in range(RUNS):
            started = time.perf_counter()
            first = None
            with client.stream("POST", url, json=BODY, headers=headers) as r:
                for line in r.iter_lines():
                    if first is None and line.startswith("data: {") and '"content": "' in line \
                            and '"content": ""' not in line:
                        first = time.perf_counter() - started
            total.append(time.perf_counter() - started)
            ttft.append(first)
    return statistics.median(ttft) * 1000, statistics.median(total) * 1000


def throughput():
    text = (PII_REPLY + " Lorem ipsum dolor sit amet, consectetur adipiscing elit. ") * 2_000
    for size in (4, 64):
        r = StreamRedactor()
        started = time.perf_counter()
        for i in range(0, len(text), size):
            r.feed(text[i:i + size])
        r.flush()
        elapsed = time.perf_counter() - started
        print(f"  {size:>3}-char chunks: {len(text) / elapsed / 1e6:6.1f} MB/s, "
              f"{len(text) / size / elapsed / 1e3:7.0f}k chunks/s, max held {r.max_held} chars")


def main():
    print(f"time to first token, median of {RUNS} streams (mock provider, 1-6 char chunks, 2ms apart)\n")
    with serving(mock_provider("bench", delay_ms=2)) as provider:
        rows = [("provider direct", provider + "/v1/chat/completions")]
        for label, redact in (("gateway, redaction off", False), ("gateway, redaction on", True)):
            config = Config(providers=[Provider("bench", provider + "/v1")], tenants={"tg-bench": "bench"},
                            db_path=tempfile.mktemp(suffix=".sqlite3"), redact=redact, tpm_limit=10_000_000)
            rows.append((label, config))

        results = []
        for label, target in rows:
            if isinstance(target, Config):
                with serving(create_app(target)) as gw:
                    results.append((label, *measure(gw + "/v1/chat/completions",
                                                    {"authorization": "Bearer tg-bench"})))
            else:
                results.append((label, *measure(target, {})))

    base = results[0][1]
    for label, ttft, total in results:
        extra = "" if label == "provider direct" else f"  (+{ttft - base:.1f} ms)"
        print(f"  {label:<24} ttft {ttft:6.1f} ms   full stream {total:7.1f} ms{extra}")
    print("\nredactor alone, in-process")
    throughput()
    print(json.dumps({"ttft_ms": {label: round(t, 2) for label, t, _ in results}}))


if __name__ == "__main__":
    main()
