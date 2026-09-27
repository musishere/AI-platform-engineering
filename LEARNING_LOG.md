# Learning Log

## 2026-09-27: Project 1, Week 1 (Days 1–5)
- **Built:** FastAPI gateway that forwards `POST /v1/messages` to Claude (via OpenRouter), checks tenant keys (SHA-256 hashed, 401 on unknown), and meters every call into `usage_events` (tokens, cost, provider cost, latency) with a before-and-after write. Anthropic SDK works through it unchanged.
- **Learned:** key = proof, tenant id = identity; fast hash is safe only for random keys; fail closed for auth/before spending, fail open after money is spent; Decimal for money; reconciliation (our cost vs provider cost); header allowlists in both directions.
- **Broke:** `"stream": true` calls went through unmetered (row 5: 200 with NULL tokens). Fixed by refusing streaming until the stretch goal. Also fixed: no body size limit, dropped `retry-after` header, missing default `anthropic-version`.
- **Next:** ~~decide on streaming~~ done, see below.

## 2026-09-27: Project 1 stretch goal, streaming
- **Built:** streamed replies pass through piece by piece while the gateway reads token counts from the SSE events. Every ending is metered and labelled in a new `outcome` column: `complete`, `client_disconnected` (output tokens estimated at ~4 chars/token) or `upstream_error`.
- **Learned:** SSE event shape (input tokens first, final output tokens last); a cancelled async task gets cancelled again at its next `await`, so cleanup must be shielded; closing the upstream is what stops Claude generating; estimate + flag beats NULL, which would let a tenant get output tokens free by disconnecting.
- **Broke:** nothing in the gateway. A test of mine broke the fake stream one event too early; the gateway's estimate was correct.
- **Next:** write the Project 1 note, then start Project 2 (Redis quotas and rate limits).

## 2026-09-28: Project 2, quotas and rate limits (Redis)
- **Built:** per-tenant token bucket ("ticket jar") and monthly token quota in Redis, both atomic Lua scripts. The quota uses a "card hold": reserve estimated tokens up front, settle to the real count after. Counters rebuild from Postgres (real tokens + in-flight holds) if Redis loses them. Redis down → 503 (fail closed, my call). Keys prefixed `gw:`.
- **Learned:** Redis runs one command at a time, and Lua bundles several steps into one; leases vs counters; "reset" by changing the key name each month; one start time must decide the month everywhere; NX for rebuild races.
- **Broke:** "check before, add after" let 20 simultaneous requests all see the old count (up to 20 calls of overshoot). Fixed with the hold: 20 at once with 100 left → 4 allowed, 0 overshoot. Also fixed: backwards clock could remove tickets, calls across midnight were counted in two months, keys could clash with another app's.
- **Next:** write the Project 1 and Project 2 notes, then Project 3 (Terraform, EKS, Argo CD, CI/CD).
