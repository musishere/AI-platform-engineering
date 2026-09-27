# Metering: records who called, what it cost, and how long it took.
#
# Two writes per call. start() runs BEFORE we forward, while nothing has been
# spent, so if it fails we can safely refuse. finish() runs AFTER, when money
# is already spent, so if it fails we still return the answer: failing would
# make the caller retry and pay twice. The row from start() stays behind with
# status_code NULL, so the call is never invisible.

import json
import logging
from decimal import Decimal

import asyncpg

from gateway.db import DB_ERRORS

log = logging.getLogger("gateway.metering")

# USD per million tokens: (input, output). Update by hand when adding a model.
# A model missing here gets cost NULL, so `WHERE cost_usd IS NULL` finds it.
# Decimal, not float: float can't hold 0.000014 exactly, and tiny errors add
# up across millions of rows.
PRICES_PER_MTOK = {
    "anthropic/claude-haiku-4.5": (Decimal("1.00"), Decimal("5.00")),
}
MILLION = Decimal(1_000_000)


def requested_model(body: bytes) -> str | None:
    # Read only the model name; the body itself is still forwarded untouched.
    try:
        model = json.loads(body).get("model")
    except (ValueError, AttributeError):
        return None
    return model if isinstance(model, str) else None


async def start(pool: asyncpg.Pool, tenant_id: int, model: str | None) -> int:
    return await pool.fetchval(
        "INSERT INTO usage_events (tenant_id, model) VALUES ($1, $2) RETURNING id",
        tenant_id,
        model,
    )


def read_usage(status_code: int, body: bytes) -> tuple[int | None, int | None, Decimal | None]:
    # Only a successful reply carries usage. Errors get NULL tokens, not 0:
    # "no reply" and "a reply that used 0 tokens" are different facts.
    if status_code != 200:
        return None, None, None
    try:
        usage = json.loads(body)["usage"]
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
    except (ValueError, KeyError, TypeError):
        return None, None, None
    # OpenRouter adds "cost"; Anthropic direct doesn't. str() first so the
    # float's binary noise doesn't leak into the Decimal.
    provider_cost = usage.get("cost")
    return (
        input_tokens,
        output_tokens,
        Decimal(str(provider_cost)) if provider_cost is not None else None,
    )


def compute_cost(model: str | None, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    # ponytail: ignores prompt-cache tokens (billed at different rates), so
    # cost is low for tenants using caching; reconciliation will show the gap.
    # Price cache_creation/cache_read tokens once a tenant uses caching.
    input_price, output_price = price
    return (input_tokens * input_price + output_tokens * output_price) / MILLION


async def finish(
    pool: asyncpg.Pool,
    event_id: int,
    model: str | None,
    status_code: int,
    body: bytes,
    latency_ms: int,
) -> None:
    input_tokens, output_tokens, provider_cost = read_usage(status_code, body)
    try:
        await pool.execute(
            """
            UPDATE usage_events
            SET status_code = $2, input_tokens = $3, output_tokens = $4,
                cost_usd = $5, provider_cost_usd = $6, latency_ms = $7
            WHERE id = $1
            """,
            event_id,
            status_code,
            input_tokens,
            output_tokens,
            compute_cost(model, input_tokens, output_tokens),
            provider_cost,
            latency_ms,
        )
    except DB_ERRORS:
        # Fail open: the money is spent and the caller should get what it paid
        # for. The row keeps status_code NULL, so this call still shows up as
        # "started, never finished".
        log.exception("metering: finishing usage_event %s failed", event_id)
