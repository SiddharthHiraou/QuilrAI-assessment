"""Bearer token -> principal.

Tokens are JWTs (HS256 here, to keep the demo self-contained) carrying sub and
role. The algorithm list is pinned, so neither "alg": "none" nor an RS/HS
confusion trick gets a token past verify. exp is required: a credential an
agent holds should not be valid forever.

Swapping in a real IdP means verifying against its JWKS instead of a shared
secret. That's this module and nothing else.
"""

import os
import time
from dataclasses import dataclass

import jwt

from tollgate.log import Log

log = Log("mcp_gateway.auth")

ISSUER = "tollgate"
AUDIENCE = "tollgate-mcp"
DEV_SECRET = "dev-only-secret-change-me-0123456789abcdef"


def secret():
    value = os.environ.get("TOLLGATE_JWT_SECRET")
    if not value:
        return DEV_SECRET
    return value


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    tenant: str | None = None


class AuthError(Exception):
    def __init__(self, error, description):
        super().__init__(description)
        self.error = error  # RFC 6750 error code, or None when no token was sent
        self.description = description

    def www_authenticate(self):
        if self.error is None:
            return 'Bearer realm="tollgate"'
        return f'Bearer realm="tollgate", error="{self.error}", error_description="{self.description}"'


def authenticate(header, key=None):
    if not header:
        raise AuthError(None, "missing bearer token")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("invalid_request", "expected 'Authorization: Bearer <token>'")
    try:
        claims = jwt.decode(
            token.strip(),
            key or secret(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "iat"]},
            leeway=30,  # clock skew between hosts is the most common "token expired" ticket
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("invalid_token", "token expired") from None
    except jwt.InvalidTokenError as exc:
        log.info("token rejected", reason=type(exc).__name__)
        raise AuthError("invalid_token", "token is invalid") from None

    role = claims.get("role")
    if not isinstance(role, str) or not role:
        raise AuthError("invalid_token", "token has no role claim")
    return Principal(subject=str(claims["sub"]), role=role, tenant=claims.get("tenant"))


def mint(subject, role, ttl=3600, tenant=None, key=None):
    """Dev tokens for the demo and tests. `tollgate token --role viewer` calls this."""
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": subject, "role": role, "iat": now, "exp": now + ttl}
    if tenant:
        claims["tenant"] = tenant
    return jwt.encode(claims, key or secret(), algorithm="HS256")
