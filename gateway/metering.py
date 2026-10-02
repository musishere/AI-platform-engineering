# Metering: records who called, what it cost, and how long it took.
#
# Two writes per call. start() runs BEFORE we forward, while nothing has been
# spent, so if it fails we can safely refuse. finish() runs AFTER, when money
# is already spent, so if it fails we still return the answer: failing would
# make the caller retry and pay twice. The row from start() stays behind with
# status_code NULL, so the call is never invisible.

import json
import logging
import time
from datetime import datetime
from decimal import Decimal

import asyncpg

from gateway.db import DB_ERRORS
from gateway.tracing import tracer

log = logging.getLogger("gateway.metering")

# USD per million tokens: (input, output). Update by hand when adding a model.
# A model missing here gets cost NULL, so `WHERE cost_usd IS NULL` finds it.
# Decimal, not float: float can't hold 0.000014 exactly, and tiny errors add
# up across millions of rows.
PRICES_PER_MTOK = {
    "anthropic/claude-haiku-4.5": (Decimal("1.00"), Decimal("5.00")),
}
MILLION = Decimal(1_000_000)


@tracer.start_as_current_span("metering.start")
async def start(
    pool: asyncpg.Pool, tenant_id: int, model: str | None, started_at: datetime, quota_hold: int
) -> int:
    # created_at is the gateway's start time, not Postgres's now(): the quota
    # uses the same value, so both agree on which month a call belongs to.
    # quota_hold is stored so a Redis rebuild can count in-flight calls.
    return await pool.fetchval(
        """
        INSERT INTO usage_events (tenant_id, model, created_at, quota_hold)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        tenant_id,
        model,
        started_at,
        quota_hold,
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


class StreamUsage:
    """Reads the token counts out of a streamed (SSE) reply as it passes by.

    A streamed reply is a series of events. input_tokens arrives first
    (message_start), the final output_tokens near the end (message_delta).
    We only look; the bytes go to the caller unchanged.
    """

    # Rough rule for English text: ~4 characters per token. Only used when
    # the final count never arrived (caller left, or the stream broke).
    CHARS_PER_TOKEN = 4

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.provider_cost: Decimal | None = None
        self.final_usage_seen = False  # message_delta arrived: output count is exact
        self.stopped = False           # message_stop arrived: the stream ended normally
        self.errored = False           # the provider sent an error event mid-stream
        self.chars_delivered = 0       # for the estimate if the final count never comes
        # perf_counter() when the first content arrived: time to first token.
        # Not message_start, which comes before the model writes anything.
        self.first_content_at: float | None = None
        self._partial_line = b""

    def feed(self, chunk: bytes) -> None:
        # Chunks don't line up with events: one event can be split across two
        # chunks. Keep the unfinished last line until the rest arrives.
        lines = (self._partial_line + chunk).split(b"\n")
        self._partial_line = lines.pop()
        for line in lines:
            if line.startswith(b"data:"):
                self._read_event(line[5:].strip())

    def _read_event(self, data: bytes) -> None:
        try:
            event = json.loads(data)
            kind = event.get("type")
            if kind == "message_start":
                usage = event["message"]["usage"]
                self.input_tokens = usage.get("input_tokens")
                self.output_tokens = usage.get("output_tokens")
            elif kind == "content_block_delta":
                if self.first_content_at is None:
                    self.first_content_at = time.perf_counter()
                delta = event["delta"]
                # text for answers, partial_json for tool calls, thinking for reasoning
                for field in ("text", "partial_json", "thinking"):
                    self.chars_delivered += len(delta.get(field) or "")
            elif kind == "message_delta":
                usage = event["usage"]
                self.output_tokens = usage["output_tokens"]
                self.final_usage_seen = True
                # Newer API versions repeat the input count here; take it if so.
                self.input_tokens = usage.get("input_tokens", self.input_tokens)
                if usage.get("cost") is not None:
                    self.provider_cost = Decimal(str(usage["cost"]))
            elif kind == "message_stop":
                self.stopped = True
            elif kind == "error":
                self.errored = True
        except (ValueError, KeyError, TypeError, AttributeError):
            # One unreadable event must never break the caller's stream.
            pass

    def usage(self) -> tuple[int | None, int | None]:
        if self.final_usage_seen:
            return self.input_tokens, self.output_tokens
        # The final count never came. Estimate from what we delivered, rounded
        # up, rather than record nothing: NULL would let a tenant get output
        # tokens free by disconnecting just before the end.
        estimate = -(-self.chars_delivered // self.CHARS_PER_TOKEN)  # ceiling division
        return self.input_tokens, max(self.output_tokens or 0, estimate)


async def finish_buffered(
    pool: asyncpg.Pool,
    event_id: int,
    model: str | None,
    status_code: int,
    body: bytes,
    latency_ms: int,
) -> tuple[int, int]:
    # A normal (non-streamed) reply: we read the whole body, so it's complete.
    input_tokens, output_tokens, provider_cost = read_usage(status_code, body)
    return await finish(
        pool, event_id, model, status_code, latency_ms,
        input_tokens, output_tokens, provider_cost, "complete",
    )


@tracer.start_as_current_span("metering.finish")
async def finish(
    pool: asyncpg.Pool,
    event_id: int,
    model: str | None,
    status_code: int,
    latency_ms: int,
    input_tokens: int | None,
    output_tokens: int | None,
    provider_cost: Decimal | None,
    outcome: str,
) -> tuple[int, int]:
    """Writes the row; returns (input, output) tokens for the quota and metrics."""
    try:
        await pool.execute(
            """
            UPDATE usage_events
            SET status_code = $2, input_tokens = $3, output_tokens = $4,
                cost_usd = $5, provider_cost_usd = $6, latency_ms = $7,
                outcome = $8
            WHERE id = $1
            """,
            event_id,
            status_code,
            input_tokens,
            output_tokens,
            compute_cost(model, input_tokens, output_tokens),
            provider_cost,
            latency_ms,
            outcome,
        )
    except DB_ERRORS:
        # Fail open: the money is spent and the caller should get what it paid
        # for. The row keeps status_code NULL, so this call still shows up as
        # "started, never finished".
        log.exception("metering: finishing usage_event %s failed", event_id)
    return input_tokens or 0, output_tokens or 0
