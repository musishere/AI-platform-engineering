-- Usage events: one row per call a known tenant makes through the gateway.
--
-- Written in two steps: INSERT before we forward (so a call is never
-- invisible) and UPDATE after (tokens, cost, latency). A row whose
-- status_code is still NULL means the call started but never finished
-- (crash, or the final write failed): it's visible, just missing numbers.
--
-- Apply: psql "$DATABASE_URL" -f db/002_usage_events.sql

CREATE TABLE usage_events (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The tenant's identity, never the key: keys rotate, history must not.
    tenant_id         bigint      NOT NULL REFERENCES tenants(id),
    model             text,
    status_code       int,
    input_tokens      int,
    output_tokens     int,
    -- From our own price table, stored at call time so a later price change
    -- doesn't rewrite history. NULL = model not in the table, never a fake 0.
    cost_usd          numeric(12, 8),
    -- What the provider says it charged, when it tells us. Compared against
    -- cost_usd to catch a stale price table (reconciliation).
    provider_cost_usd numeric(12, 8),
    latency_ms        int,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- For "how much did tenant X use this month?"
CREATE INDEX ON usage_events (tenant_id, created_at);
