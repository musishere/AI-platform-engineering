# Auth: decides which tenant is calling, from the API key it sends.
#
# The key is proof, not identity: we turn it into a tenant id at the front
# door, and everything after (forwarding, metering) only sees that id.
# Keys are stored as SHA-256 hashes, so a database leak leaks no working keys.

import hashlib
import secrets

import asyncpg

from gateway.tracing import tracer

# The prefix makes a key recognisable by eye and by secret scanners (e.g.
# GitHub's), so a key pasted somewhere public gets caught.
KEY_PREFIX = "gw_"


def generate_key() -> str:
    # 32 random bytes = 256 bits. Unguessable, and that is exactly what makes
    # a fast hash safe here. If tenants chose their own keys we'd need bcrypt.
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # Fast and deterministic (same key -> same hash), so the lookup is one
    # indexed query. bcrypt would add ~100ms per request and, being salted,
    # couldn't be looked up by value at all.
    return hashlib.sha256(key.encode()).hexdigest()


@tracer.start_as_current_span("auth.find_tenant")
async def find_tenant(pool: asyncpg.Pool, key: str | None) -> asyncpg.Record | None:
    # Returns the tenant's id plus its limits, or None for an unknown key.
    # One query for both, so adding limits cost no extra round trip.
    if not key:
        return None
    return await pool.fetchrow(
        """
        SELECT id, burst, refill_per_second, monthly_token_quota
        FROM tenants WHERE api_key_hash = $1
        """,
        hash_key(key),
    )
