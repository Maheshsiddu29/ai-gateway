"""Authentication of callers of the runtime's API.

The runtime has its own credentials (``AGENT_API_KEYS``), independent of the
gateway's. A runtime caller never holds the gateway key the runtime uses, and the
key list can later be replaced by another ``Authenticator`` without changing
request handling.
"""

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. ``id`` is a truncated SHA-256 of the key, safe to log."""

    id: str


class Authenticator(Protocol):
    def authenticate(self, credential: str) -> Principal | None: ...


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class StaticKeyAuthenticator:
    def __init__(self, api_keys: Iterable[SecretStr]) -> None:
        self._entries = [
            (digest, Principal(id=f"agent-key-{digest.hex()[:12]}"))
            for digest in (_digest(key.get_secret_value()) for key in api_keys)
        ]

    def authenticate(self, credential: str) -> Principal | None:
        # Fixed-length digests compared with compare_digest, checking every key
        # without returning early, so timing does not reveal which key matched.
        candidate = _digest(credential)
        matched: Principal | None = None
        for digest, principal in self._entries:
            if hmac.compare_digest(digest, candidate):
                matched = principal
        return matched


def parse_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
