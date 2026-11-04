"""Environment config for the LLM gateway. Defaults point at the local mocks."""

import os
from dataclasses import dataclass, field

from tollgate.router.router import Provider

DEV_TENANT_KEYS = "tg-dev-acme:acme,tg-dev-globex:globex"


def _providers():
    out = []
    for slot, port in (("PRIMARY", 8831), ("SECONDARY", 8832)):
        url = os.environ.get(f"TOLLGATE_{slot}_BASE_URL", f"http://127.0.0.1:{port}/v1")
        if not url:
            continue
        out.append(Provider(
            name=os.environ.get(f"TOLLGATE_{slot}_NAME", slot.lower()),
            base_url=url,
            api_key=os.environ.get(f"TOLLGATE_{slot}_API_KEY") or None,
            model=os.environ.get(f"TOLLGATE_{slot}_MODEL") or None,
            timeout=float(os.environ.get("TOLLGATE_PROVIDER_TIMEOUT_S", "3.0")),
        ))
    return out


def _tenants():
    raw = os.environ.get("TOLLGATE_TENANT_KEYS", DEV_TENANT_KEYS)
    pairs = (item.split(":", 1) for item in raw.split(",") if ":" in item)
    return {key.strip(): name.strip() for key, name in pairs}


@dataclass
class Config:
    providers: list = field(default_factory=_providers)
    tenants: dict = field(default_factory=_tenants)
    tpm_limit: int = field(default_factory=lambda: int(os.environ.get("TOLLGATE_TPM_LIMIT", "50000")))
    db_path: str = field(default_factory=lambda: os.environ.get("TOLLGATE_DB", "data/ratelimit.sqlite3"))
    redact: bool = field(default_factory=lambda: os.environ.get("TOLLGATE_REDACT", "1") != "0")
    labeled: bool = field(default_factory=lambda: os.environ.get("TOLLGATE_LABELED_REDACTIONS") == "1")
    idle_timeout: float = field(default_factory=lambda: float(os.environ.get("TOLLGATE_STREAM_IDLE_S", "30")))
