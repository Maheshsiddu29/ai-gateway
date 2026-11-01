"""Gateway API-key authentication.

The rest of the request pipeline depends only on ``Principal`` and the
``Authenticator`` protocol, so the static key list can later be replaced by a
tenant-aware store without changing request handling.
"""

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    ``id`` is safe to log: for static keys it is a truncated SHA-256 fingerprint,
    which identifies the key without revealing it.
    """

    id: str


class Authenticator(Protocol):
    def authenticate(self, credential: str) -> Principal | None: ...


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class StaticKeyAuthenticator:
    def __init__(self, api_keys: Iterable[SecretStr], id_prefix: str = "key") -> None:
        self._entries = [
            (digest, Principal(id=f"{id_prefix}-{digest.hex()[:12]}"))
            for digest in (_digest(key.get_secret_value()) for key in api_keys)
        ]

    def authenticate(self, credential: str) -> Principal | None:
        # Comparing fixed-length digests with compare_digest, and checking every
        # configured key without returning early, keeps the time taken independent
        # of which key (if any) matched and of how much of it matched.
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
