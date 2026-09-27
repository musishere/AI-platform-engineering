-- The quota "card hold" taken when the call started (estimated tokens).
--
-- Stored on the row so Postgres can rebuild the Redis counter exactly: a
-- finished call counts its real tokens, a call still in flight (status_code
-- NULL) counts its hold, just like the live counter does. A call that crashed
-- and never finished keeps counting its hold, which errs on the safe side.
--
-- Apply: psql "$DATABASE_URL" -f db/005_usage_quota_hold.sql

ALTER TABLE usage_events ADD COLUMN quota_hold int;
