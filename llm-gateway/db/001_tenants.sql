-- Tenants: the callers allowed to use the gateway (a team, an app, a customer).
--
-- `id` is the tenant's identity and never changes; later tables (usage_events)
-- point at it. The API key is only proof of identity, so we store just its
-- SHA-256 hash: a leaked database contains no working keys.
--
-- Apply: psql "$DATABASE_URL" -f db/001_tenants.sql

CREATE TABLE tenants (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text        NOT NULL UNIQUE,
    -- UNIQUE also creates the index that makes the per-request lookup fast.
    api_key_hash text        NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now()
);
