-- How each call ended, so exact rows can be told apart from estimated ones:
--   complete            - reply fully read; token counts are exact
--                         (or NULL because there was no reply, e.g. a 400)
--   client_disconnected - caller left mid-stream; output_tokens is estimated
--   upstream_error      - the stream broke on the provider's side midway;
--                         output_tokens is estimated
-- NULL on rows written before this column existed.
--
-- Apply: psql "$DATABASE_URL" -f db/003_usage_outcome.sql

ALTER TABLE usage_events ADD COLUMN outcome text;
