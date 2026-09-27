# Limits: stops a tenant calling too fast (rate limit) or too much (monthly
# token quota), before any money is spent.
#
# Both live in Redis, not in Python memory: once the gateway runs as several
# copies (Project 3), a per-process dict would give each copy its own limit.
# Redis is one shared jar every copy draws from.
#
# Redis down means everyone is refused (fail closed), same as auth: without
# limits, a runaway tenant could spend without bound.

import logging
import math
import os
from datetime import datetime, timedelta

import asyncpg
import redis.asyncio as redis

log = logging.getLogger("gateway.limits")

# Token bucket ("ticket jar"), run inside Redis as one atomic step. Doing
# read -> refill -> take in Python would let two concurrent requests both see
# "1 ticket left" and both pass. Redis runs a script with nothing in between.
# Uses Redis's clock, not ours: clocks on different gateway copies drift.
TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state = redis.call('HMGET', KEYS[1], 'tickets', 'ts')
local tickets = tonumber(state[1]) or capacity   -- new tenant: full jar
local ts = tonumber(state[2]) or now

-- Refill for the time since the last request, never above the jar size.
-- If the clock jumped backwards, elapsed would be negative and we'd take
-- tickets away for nothing. Treat that as zero time passed instead.
local elapsed = math.max(0, now - ts)
tickets = math.min(capacity, tickets + elapsed * rate)

local wait = 0
if tickets >= 1 then
  tickets = tickets - 1
else
  wait = (1 - tickets) / rate                    -- seconds until 1 ticket
end
redis.call('HSET', KEYS[1], 'tickets', tickets, 'ts', now)
-- A full jar needs no state: expire once it would have refilled anyway.
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 1)
-- Lua numbers come back to Redis as integers, so send the wait as a string.
return tostring(wait)
"""

# The "card hold", in one atomic step: if the tenant is under quota, hold the
# estimated tokens on the counter right away, so a request arriving at the
# same moment already sees them as used. Never holds more than what's left:
# a big estimate (say, a large image) then just takes the rest rather than
# being refused while the tenant still has allowance.
# Returns the hold, -1 if already over quota, or 'missing' if the counter
# doesn't exist yet (Python rebuilds it from Postgres and tries again).
RESERVE = """
local used = redis.call('GET', KEYS[1])
if not used then return 'missing' end
used = tonumber(used)
local quota = tonumber(ARGV[1])
if used >= quota then return -1 end
local hold = math.min(tonumber(ARGV[2]), quota - used)
redis.call('INCRBY', KEYS[1], hold)
return hold
"""

# Swap the hold for the real count: add (real - hold), which is negative when
# the call used less than we held. Only if the counter still exists: if Redis
# lost it, the rebuild from Postgres already has this call's real tokens.
ADD_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[1])
end
return nil
"""

# The output guess for the hold: a typical answer, not max_tokens. Holding the
# maximum would refuse a tenant with 1,000 left for asking max_tokens 4000.
TYPICAL_OUTPUT_TOKENS = 500

# Every gateway key starts with this, like its own folder, so it can't
# collide with another app sharing the same Redis.
KEY_PREFIX = "gw:"

# Longer than any month, so a counter outlives its month and then cleans up.
QUOTA_KEY_TTL = timedelta(days=40)

# What a broken Redis or Postgres looks like here; both mean "refuse".
LIMIT_ERRORS = (redis.RedisError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


def create_client() -> redis.Redis:
    # Short timeouts: every request waits on Redis, so a hung Redis must fail
    # fast (and be refused) rather than make every caller hang.
    return redis.from_url(
        os.environ["REDIS_URL"], socket_timeout=1, socket_connect_timeout=1
    )


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    # Day 28 exists in every month, so +4 days always lands in the next one.
    return month_start(month_start(now).replace(day=28) + timedelta(days=4))


def estimate_tokens(body_bytes: int, max_tokens: object) -> int:
    # Input: ~4 characters per token over the whole request body (JSON syntax
    # included, so a slight overestimate). Output: a typical answer length,
    # or max_tokens if the caller asked for less.
    output = TYPICAL_OUTPUT_TOKENS
    if isinstance(max_tokens, int) and 0 < max_tokens < output:
        output = max_tokens
    return math.ceil(body_bytes / 4) + output


def seconds_until_next_month(started_at: datetime) -> int:
    return math.ceil((next_month_start(started_at) - started_at).total_seconds())


class Limiter:
    def __init__(self, client: redis.Redis) -> None:
        self.redis = client
        self._take_ticket = client.register_script(TOKEN_BUCKET)
        self._reserve = client.register_script(RESERVE)
        self._add_if_exists = client.register_script(ADD_IF_EXISTS)

    async def rate_limit(self, tenant: asyncpg.Record) -> int | None:
        """None if a ticket was taken, else seconds until the next ticket."""
        wait = float(
            await self._take_ticket(
                keys=[f"{KEY_PREFIX}bucket:{tenant['id']}"],
                args=[tenant["burst"], tenant["refill_per_second"]],
            )
        )
        # retry-after must be whole seconds; round up so a caller that waits
        # exactly that long finds a ticket.
        return math.ceil(wait) if wait > 0 else None

    async def reserve(
        self, db: asyncpg.Pool, tenant: asyncpg.Record, started_at: datetime, estimate: int
    ) -> int | None:
        """Holds tokens on the monthly counter. Returns the hold, or None if over quota.

        started_at is the call's one start time. The same value picks the
        month here, is stored on the usage row, and is used by settle(), so a
        call running across midnight on the 1st counts in the month it started,
        in Postgres and in Redis alike.
        """
        key = self._quota_key(tenant["id"], started_at)
        # Two tries: the counter can be missing on the first (new month, or
        # Redis lost it). After rebuilding, it's there for the second.
        for _ in range(2):
            result = await self._reserve(
                keys=[key], args=[tenant["monthly_token_quota"], estimate]
            )
            if result != b"missing":
                return None if int(result) < 0 else int(result)
            await self._rebuild(db, tenant["id"], started_at)
        raise redis.RedisError(f"quota counter {key} vanished right after rebuild")

    async def _rebuild(self, db: asyncpg.Pool, tenant_id: int, started_at: datetime) -> None:
        # Recount from usage_events, the source of truth, so a Redis restart
        # can't quietly reset anyone's quota to zero. Counts exactly what the
        # live counter would hold: real tokens for finished calls, the hold for
        # calls still in flight (status_code NULL).
        used = await db.fetchval(
            """
            SELECT coalesce(sum(CASE
                       WHEN status_code IS NULL THEN coalesce(quota_hold, 0)
                       ELSE coalesce(input_tokens, 0) + coalesce(output_tokens, 0)
                   END), 0)
            FROM usage_events
            WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
            """,
            tenant_id,
            month_start(started_at),
            next_month_start(started_at),
        )
        # nx: if another request rebuilt it a moment ago, keep that one.
        await self.redis.set(
            self._quota_key(tenant_id, started_at), used, nx=True, ex=QUOTA_KEY_TTL
        )

    async def settle(
        self, tenant_id: int, started_at: datetime, hold: int, actual: int
    ) -> None:
        """Swaps the hold for the real token count once the call is over.

        actual=0 simply gives the whole hold back (call never happened).
        """
        if actual == hold:
            return
        try:
            await self._add_if_exists(
                keys=[self._quota_key(tenant_id, started_at)], args=[actual - hold]
            )
        except LIMIT_ERRORS:
            # After the call the money is already spent, so never fail the
            # caller here. The row in Postgres still has the tokens; the
            # counter is just off until it's next rebuilt.
            log.exception(
                "limits: settling tenant %s (hold %s, actual %s) failed", tenant_id, hold, actual
            )

    @staticmethod
    def _quota_key(tenant_id: int, started_at: datetime) -> str:
        return f"{KEY_PREFIX}quota:{tenant_id}:{started_at:%Y-%m}"
