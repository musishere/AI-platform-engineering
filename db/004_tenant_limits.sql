-- Per-tenant limits, read in the same query as the API key check (no extra
-- round trip per request).
--
--   burst               - token bucket size: requests allowed at once
--   refill_per_second   - bucket refill rate: the long-run average request rate
--   monthly_token_quota - input + output tokens allowed per calendar month (UTC)
--
-- Apply: psql "$DATABASE_URL" -f db/004_tenant_limits.sql

ALTER TABLE tenants
    ADD COLUMN burst               int              NOT NULL DEFAULT 20      CHECK (burst > 0),
    ADD COLUMN refill_per_second   double precision NOT NULL DEFAULT 1       CHECK (refill_per_second > 0),
    ADD COLUMN monthly_token_quota bigint           NOT NULL DEFAULT 1000000 CHECK (monthly_token_quota >= 0);
