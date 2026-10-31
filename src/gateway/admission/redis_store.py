"""Atomic admission state in Redis.

Each gateway key has four kinds of state, all sharing the hash tag ``{<principal id>}``
so that they live in one Redis Cluster slot and a single script may touch them:

* ``...:requests`` and ``...:tokens``: token buckets (hashes of ``tokens`` and
  ``updated_ms``). An absent bucket is full; buckets expire once they would have
  refilled, so idle keys cost no memory.
* ``...:leases``: a sorted set of active reservation IDs scored by lease expiry, used
  for the concurrency limit. Expired members are removed on the next admission.
* ``...:reservation:<id>``: a hash recording the reservation state (``admitted``,
  ``settled``, or ``released``) and the reserved token count, kept for the lease
  lifetime plus a retention period.

Two scripts operate on this state. ``admit`` checks every limit and either commits
all of them or changes nothing. ``settle`` releases the concurrency lease and
replaces the token reservation with the tokens actually charged; it is idempotent.
Both read the clock with ``TIME`` on the Redis server, so gateway clocks need not
agree.

Key names contain only the principal ID (a truncated digest of the gateway key) and
gateway-generated reservation IDs, never key material.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

from redis.asyncio import Redis

from gateway.admission.limits import KeyLimits, TokenBucket

KEY_PREFIX = "agw:v1"
_PRINCIPAL_ID = re.compile(r"key-[0-9a-f]{12}")
_RESERVATION_ID = re.compile(r"[0-9a-f]{32}")

# Shared helpers. Bucket values are stored with fixed precision so a long-lived
# key never accumulates floating-point formatting artefacts.
_LUA_PRELUDE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)

local function available(key, rate, capacity)
  local stored = redis.call('HMGET', key, 'tokens', 'updated_ms')
  local tokens, updated = tonumber(stored[1]), tonumber(stored[2])
  if tokens == nil or updated == nil then
    return capacity
  end
  -- A server clock that moved backwards (for example after failover) refills nothing.
  local elapsed = math.max(0, now - updated)
  return math.min(capacity, tokens + elapsed * rate / 60000)
end

local function refill_ms(missing, rate)
  return math.ceil(missing * 60000 / rate)
end

local function save(key, tokens, rate, capacity)
  redis.call('HSET', key, 'tokens', string.format('%.6f', tokens), 'updated_ms', now)
  -- An absent bucket is a full one, so the key can expire once it would be full.
  redis.call('PEXPIRE', key, refill_ms(capacity - tokens, rate) + 1000)
end
"""

# KEYS: request bucket, token bucket, leases, reservation record.
# ARGV: reservation id, request rate, request capacity, token rate, token capacity,
#       tokens requested, max concurrent, lease ttl ms, record ttl ms.
# A rate or limit of 0 means the dimension is not limited.
_ADMIT = (
    _LUA_PRELUDE
    + """
local id = ARGV[1]
local request_rate, request_capacity = tonumber(ARGV[2]), tonumber(ARGV[3])
local token_rate, token_capacity = tonumber(ARGV[4]), tonumber(ARGV[5])
local requested = tonumber(ARGV[6])
local max_concurrent = tonumber(ARGV[7])
local lease_ttl, record_ttl = tonumber(ARGV[8]), tonumber(ARGV[9])

local state = redis.call('HGET', KEYS[4], 'state')
if state == 'released' then
  return {0, 'released', -1}
end
if state then
  return {1, 'duplicate', -1}
end

redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)

local request_tokens, token_tokens
if request_rate > 0 then
  request_tokens = available(KEYS[1], request_rate, request_capacity)
  if request_tokens < 1 then
    return {0, 'requests', refill_ms(1 - request_tokens, request_rate)}
  end
end
if token_rate > 0 then
  token_tokens = available(KEYS[2], token_rate, token_capacity)
  if token_tokens < requested then
    return {0, 'tokens', refill_ms(requested - token_tokens, token_rate)}
  end
end
if max_concurrent > 0 and redis.call('ZCARD', KEYS[3]) >= max_concurrent then
  return {0, 'concurrency', -1}
end

if request_rate > 0 then
  save(KEYS[1], request_tokens - 1, request_rate, request_capacity)
end
if token_rate > 0 then
  save(KEYS[2], token_tokens - requested, token_rate, token_capacity)
end
redis.call('ZADD', KEYS[3], now + lease_ttl, id)
if redis.call('PTTL', KEYS[3]) < lease_ttl then
  redis.call('PEXPIRE', KEYS[3], lease_ttl)
end
redis.call('HSET', KEYS[4], 'state', 'admitted', 'tokens', requested)
redis.call('PEXPIRE', KEYS[4], record_ttl)
return {1, 'admitted', -1}
"""
)

# KEYS: token bucket, leases, reservation record.
# ARGV: reservation id, tokens charged, token rate, token capacity, record ttl ms.
_SETTLE = (
    _LUA_PRELUDE
    + """
local id = ARGV[1]
local charged = tonumber(ARGV[2])
local token_rate, token_capacity = tonumber(ARGV[3]), tonumber(ARGV[4])
local record_ttl = tonumber(ARGV[5])

redis.call('ZREM', KEYS[2], id)
local state = redis.call('HGET', KEYS[3], 'state')
if not state then
  -- The admission never committed, or its record has expired. Refund nothing, and
  -- record the release so that an admission with this ID that is still in flight
  -- cannot commit afterwards.
  redis.call('HSET', KEYS[3], 'state', 'released')
  redis.call('PEXPIRE', KEYS[3], record_ttl)
  return 'missing'
end
if state ~= 'admitted' then
  return 'duplicate'
end

if token_rate > 0 then
  local reserved = tonumber(redis.call('HGET', KEYS[3], 'tokens')) or 0
  local tokens = available(KEYS[1], token_rate, token_capacity)
  -- Usage above the reservation becomes debt that delays later admissions. Debt is
  -- capped at one bucket capacity.
  local settled = math.max(-token_capacity, math.min(token_capacity, tokens + reserved - charged))
  save(KEYS[1], settled, token_rate, token_capacity)
end
redis.call('HSET', KEYS[3], 'state', 'settled', 'charged', charged)
return 'settled'
"""
)


class Rejection(StrEnum):
    REQUESTS = "requests"
    TOKENS = "tokens"
    CONCURRENCY = "concurrency"
    # A settlement for this reservation ID ran first (see the settle script).
    RELEASED = "released"


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    rejection: Rejection | None = None
    retry_after_ms: int | None = None


class SettleOutcome(StrEnum):
    SETTLED = "settled"
    DUPLICATE = "duplicate"
    MISSING = "missing"


@dataclass(frozen=True)
class Admission:
    principal_id: str
    reservation_id: str
    limits: KeyLimits
    tokens: int
    lease_ttl_ms: int
    record_ttl_ms: int


class RedisAdmissionStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        # Scripts are invoked with EVALSHA. If the server has no cached copy (after a
        # restart or SCRIPT FLUSH), redis-py loads it and calls EVALSHA again; that is
        # safe because a NOSCRIPT error means nothing ran.
        self._admit = redis.register_script(_ADMIT)
        self._settle = redis.register_script(_SETTLE)

    async def admit(self, admission: Admission) -> AdmissionDecision:
        keys = _key_names(admission.principal_id, admission.reservation_id)
        reply: list[Any] = await self._admit(
            keys=[keys.requests, keys.tokens, keys.leases, keys.reservation],
            args=[
                admission.reservation_id,
                *_bucket_args(admission.limits.requests),
                *_bucket_args(admission.limits.tokens),
                admission.tokens,
                admission.limits.max_concurrent or 0,
                admission.lease_ttl_ms,
                admission.record_ttl_ms,
            ],
        )
        admitted, reason, retry_after_ms = reply
        if admitted:
            return AdmissionDecision(admitted=True)
        return AdmissionDecision(
            admitted=False,
            rejection=Rejection(reason),
            retry_after_ms=int(retry_after_ms) if retry_after_ms >= 0 else None,
        )

    async def settle(self, admission: Admission, charged_tokens: int) -> SettleOutcome:
        keys = _key_names(admission.principal_id, admission.reservation_id)
        reply = await self._settle(
            keys=[keys.tokens, keys.leases, keys.reservation],
            args=[
                admission.reservation_id,
                charged_tokens,
                *_bucket_args(admission.limits.tokens),
                admission.record_ttl_ms,
            ],
        )
        return SettleOutcome(reply)

    async def ping(self) -> None:
        await self._redis.ping()


class _Keys(NamedTuple):
    requests: str
    tokens: str
    leases: str
    reservation: str


def _key_names(principal_id: str, reservation_id: str) -> _Keys:
    # Both IDs are generated by the gateway; validating them keeps arbitrary strings
    # (including hash-tag braces) out of key names.
    if not _PRINCIPAL_ID.fullmatch(principal_id):
        raise ValueError("invalid principal ID")
    if not _RESERVATION_ID.fullmatch(reservation_id):
        raise ValueError("invalid reservation ID")
    base = f"{KEY_PREFIX}:{{{principal_id}}}"
    return _Keys(
        requests=f"{base}:requests",
        tokens=f"{base}:tokens",
        leases=f"{base}:leases",
        reservation=f"{base}:reservation:{reservation_id}",
    )


def _bucket_args(bucket: TokenBucket | None) -> tuple[int, int]:
    if bucket is None:
        return 0, 0
    return bucket.rate_per_minute, bucket.capacity
