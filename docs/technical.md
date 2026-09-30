# Technical Reference: LLM Gateway Platform

> **Status (2026-09-30):** Projects 1 (gateway) and 2 (quotas and rate limits) are built. Project 3 (deploy)
> is in progress: the Terraform for the Oracle Cloud network and cluster is written, and the gateway runs on
> a local minikube cluster deployed by Argo CD. CI/CD is done: every code push is built, scanned, pushed to GHCR and released as a canary. The OKE
> cluster itself is blocked on cloud capacity.
>
> This file is the **deep technical walkthrough of the whole repo**: every module, the data model, every
> failure path, and the reason behind each decision. For Kubernetes internals (pods, Services, DNS, Argo CD's
> loop) see [deployment.md](deployment.md). For the plan see [ROADMAP.md](ROADMAP.md).

---

## Table of contents

1. [What the system is](#1-what-the-system-is)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [Repository layout](#3-repository-layout)
4. [Tech stack and why each piece](#4-tech-stack-and-why-each-piece)
5. [Life of one request](#5-life-of-one-request)
6. [Auth](#6-auth-gatewayauthpy)
7. [Limits: rate limit and monthly quota](#7-limits-gatewaylimitspy)
8. [Forwarding](#8-forwarding-gatewayforwardingpy)
9. [Metering](#9-metering-gatewaymeteringpy)
10. [Streaming](#10-streaming-gatewaystreamingpy)
11. [Data model](#11-data-model)
12. [Failure handling: fail open vs fail closed](#12-failure-handling-fail-open-vs-fail-closed)
13. [Error contract](#13-error-contract)
14. [Configuration](#14-configuration)
15. [Packaging: the container image](#15-packaging-the-container-image)
16. [Running on Kubernetes (minikube + Argo CD)](#16-running-on-kubernetes-minikube--argo-cd)
17. [Cloud infrastructure (Terraform on Oracle Cloud)](#17-cloud-infrastructure-terraform-on-oracle-cloud)
18. [How to run things](#18-how-to-run-things)
19. [Known limits and deliberate shortcuts](#19-known-limits-and-deliberate-shortcuts)
20. [Key decisions (interview stories)](#20-key-decisions-interview-stories)
21. [Glossary](#21-glossary)

---

## 1. What the system is

An **LLM gateway**: one service that sits between many callers ("tenants") and an LLM provider. Think of a
building's front desk. Every visitor passes it, so it is the one place to check ID, enforce house rules,
and log who came and what it cost.

It exposes the **same API as Anthropic's Messages API** (`POST /v1/messages`). An app built on the official
Anthropic SDK switches to the gateway by changing two things: the base URL and the API key. Nothing else.

What it does on every call:

| Concern | What happens | Module |
|---|---|---|
| Auth | Turns the caller's API key into a tenant id | [gateway/auth.py](../gateway/auth.py) |
| Rate limit | Stops a tenant calling too fast (token bucket in Redis) | [gateway/limits.py](../gateway/limits.py) |
| Quota | Stops a tenant using too many tokens per month (Redis counter, rebuilt from Postgres) | [gateway/limits.py](../gateway/limits.py) |
| Forwarding | Sends the request on to the provider unchanged | [gateway/forwarding.py](../gateway/forwarding.py) |
| Metering | Records tokens, cost, latency, status per call in Postgres | [gateway/metering.py](../gateway/metering.py) |
| Streaming | Relays streamed replies piece by piece while still metering them | [gateway/streaming.py](../gateway/streaming.py) |

**Upstream today:** OpenRouter (`https://openrouter.ai/api`), which speaks the Anthropic Messages format and
routes to Claude. The model used in testing is `anthropic/claude-haiku-4.5`. Switching to Anthropic directly
is a one-line header change plus env vars (see [section 8](#8-forwarding-gatewayforwardingpy)).

---

## 2. Architecture at a glance

```
                        ┌──────────────────────── LLM gateway (FastAPI, stateless) ─────────────────────────┐
  tenant app            │                                                                                   │
  (Anthropic SDK) ──────▶  POST /v1/messages                                                                │
  x-api-key: gw_…       │    1. auth        hash key → tenant row (id + limits)  ───────────▶  Postgres     │
                        │    2. rate limit  take one ticket from the tenant's bucket ───────▶  Redis        │
                        │    3. read body   (max 32 MB)                                                     │
                        │    4. quota       hold estimated tokens on the month counter ─────▶  Redis        │
                        │    5. meter #1    INSERT usage row (status NULL) ─────────────────▶  Postgres     │
                        │    6. forward     POST /v1/messages ──────────────────────────────▶  OpenRouter ─▶ Claude
                        │    7. reply       buffered, or streamed chunk by chunk ◀──────────                │
                        │    8. meter #2    UPDATE row: tokens, cost, latency, outcome ─────▶  Postgres     │
                        │    9. settle      swap the hold for the real token count ─────────▶  Redis        │
  ◀─────────────────────┤                                                                                   │
                        └───────────────────────────────────────────────────────────────────────────────────┘
```

**Two data stores, two different jobs:**

- **Postgres = source of truth.** Tenants, API key hashes, limits, one row per call. Durable. If Postgres and
  Redis ever disagree, Postgres wins.
- **Redis = fast shared counters.** Rate-limit buckets and monthly quota counters. Deliberately **not
  durable**: every quota counter can be rebuilt from Postgres, and a lost rate-limit bucket just starts full.

**The gateway process itself holds no state.** That is what makes it safe to run 2+ copies behind one
address: any copy can serve any request, because everything they need is in Postgres or Redis.

---

## 3. Repository layout

```
.
├── gateway/                  # the application (Python package)
│   ├── main.py               # FastAPI app, lifespan, the request pipeline (wires the modules together)
│   ├── auth.py               # key generation, hashing, tenant lookup
│   ├── limits.py             # token bucket + monthly quota (Redis Lua scripts), rebuild from Postgres
│   ├── forwarding.py         # upstream HTTP client, header allowlists, buffered + streamed calls
│   ├── metering.py           # usage rows, cost calculation, SSE usage parser
│   ├── streaming.py          # MeteredStream: relay SSE + guaranteed metering on any ending
│   ├── db.py                 # DB_ERRORS: what "Postgres is broken" looks like
│   └── create_tenant.py      # CLI: create a tenant, print its key once
├── db/                       # SQL migrations, applied in name order
│   ├── 001_tenants.sql
│   ├── 002_usage_events.sql
│   ├── 003_usage_outcome.sql
│   ├── 004_tenant_limits.sql
│   └── 005_usage_quota_hold.sql
├── scripts/client.py         # end-to-end test using the real Anthropic SDK
├── Dockerfile                # 2-stage image, non-root
├── k8s/                      # Kubernetes manifests (what Argo CD syncs)
├── argocd/gateway.yaml       # Argo CD Application (applied once by hand)
├── infra/
│   ├── bootstrap/            # permanent, free: compartment, state bucket, budget alerts
│   └── cluster/              # per-session: VCN, subnets, gateways, OKE cluster + node pool
├── pyproject.toml / uv.lock  # dependencies (uv)
├── deployment.md             # Kubernetes deep dive
├── ROADMAP.md                # the 8-project plan
└── LEARNING_LOG.md           # session recaps
```

**Separation rule:** `main.py` is the only file that knows the order of steps. Each other module does one job
and knows nothing about HTTP routes. Forwarding doesn't know about auth, metering doesn't know about Redis.
That keeps the pipeline readable as routing and guardrails get added in later projects.

---

## 4. Tech stack and why each piece

| Piece | Choice | Why this one | What we gave up |
|---|---|---|---|
| Web framework | **FastAPI** (on Starlette) + **uvicorn** | Async, so one process handles many slow LLM calls at once (an LLM call is mostly waiting). Starlette's `StreamingResponse` gives streaming for free. | Raw speed of Go/Rust. Irrelevant here: the gateway adds milliseconds to calls that take seconds. |
| Upstream HTTP | **httpx** `AsyncClient` | Async, supports streaming (`send(stream=True)`), connection pooling. | — |
| Postgres driver | **asyncpg** | Async, fast, built-in connection pool. No ORM: the queries are few and simple, so an ORM would only hide them. | Migrations tooling (we use numbered `.sql` files). |
| Counters | **Redis** (`redis.asyncio`) + **Lua scripts** | Shared between all gateway copies, sub-millisecond, and Lua gives atomic multi-step operations. | Durability (on purpose, see section 7). |
| Money | Python **`Decimal`** / Postgres **`numeric(12, 8)`** | Floats can't store 0.000014 exactly; errors add up over millions of rows. | Nothing meaningful. |
| Packaging | **uv** + 2-stage **Docker** image | Locked, reproducible installs (`uv.lock`); small runtime image with no build tools. | — |
| Orchestration | **Kubernetes** (minikube now, OKE later) | Runs several copies, restarts crashed ones, gives stable addresses. | Simplicity of one VM. |
| Deploy | **Argo CD** (GitOps) | The cluster pulls from Git, so Git is the single source of truth and CI never needs cluster credentials. | A push-based pipeline's simplicity. |
| Infra as code | **Terraform** (OCI provider) | Build and destroy the whole cloud setup each session, with state in a remote bucket. | — |

Dependencies (from [pyproject.toml](../pyproject.toml)): `fastapi`, `uvicorn`, `httpx`, `asyncpg`, `redis`.
Dev only: `anthropic` (for [scripts/client.py](../scripts/client.py)). Python ≥ 3.12.

---

## 5. Life of one request

This is `messages()` in [gateway/main.py](../gateway/main.py), step by step. The **order is the design**: cheap
checks first, anything that costs money last, and every step that can fail before spending money fails
*closed* (refuses).

### 5.0 Process startup (`lifespan`)

Before any request, the app creates **one of each shared client per process** and stores them on
`app.state`:

- `forwarding.create_client()`: one `httpx.AsyncClient` to the upstream. Reused so TLS connections stay open
  (a new TLS handshake costs ~100 ms).
- `asyncpg.create_pool(DATABASE_URL)`: a Postgres connection pool (asyncpg default: 10 connections).
- `limits.create_client()` wrapped in `limits.Limiter`: one Redis client, with the three Lua scripts
  registered once.

On shutdown, all three are closed. Env vars like `UPSTREAM_API_KEY` are read **at import time**, so a missing
variable crashes the process at startup instead of on the first request an hour later.

### 5.1 The pipeline

```
request
  │
  ├─ 1. auth.find_tenant(x-api-key)        DB error → 503    unknown key → 401
  │
  ├─ started_at = now (UTC)                 one timestamp decides "which month" everywhere
  │
  ├─ 2. limiter.rate_limit(tenant)          Redis error → 503   no ticket → 429 + retry-after
  │
  ├─ 3. read_body_limited()                 > 32 MB → 413
  │     parse_json_object()                 peek at model / stream / max_tokens (never modified)
  │
  ├─ 4. limiter.reserve(estimate)           Redis/DB error → 503   over quota → 429 + retry-after (to next month)
  │
  ├─ 5. metering.start()                    DB error → give hold back, 503
  │
  ├─ 6. forward
  │     ├─ stream: true  → open_stream()
  │     │     └─ 200 + text/event-stream → return MeteredStream (steps 7–9 happen when the stream ends)
  │     │     └─ anything else           → read it whole, fall through as buffered
  │     └─ otherwise     → forward_messages()
  │                                          timeout → 504    connection error → 502
  │
  ├─ 7. build Response (upstream status + body passed through, errors included)
  ├─ 8. metering.finish_buffered()          DB error → logged, caller still gets the answer
  └─ 9. limiter.settle(hold, real tokens)   Redis error → logged, caller still gets the answer
```

### 5.2 Why this order

- **Auth first**: nothing else can run without knowing the tenant (limits are per tenant).
- **Rate limit before reading the body**: a tenant being throttled should cost us as little as possible.
  Reading up to 32 MB for a request we'll refuse is waste.
- **Quota after reading the body**: the token estimate needs the body size and `max_tokens`.
- **`metering.start` before forwarding**: once we forward, money is being spent. If we can't write a record
  first, we refuse, so no call is ever invisible.
- **`metering.finish` and `settle` after**: the money is already spent. Failing now would make the caller
  retry and pay twice, so these log errors and never fail the request.

### 5.3 Small but important details in `main.py`

- **`read_body_limited`**: checks `Content-Length` first (cheap early exit), but *also* counts bytes while
  reading, because a chunked upload has no declared length. Without a cap, one tenant sending a huge body
  could exhaust memory and take down every tenant on that pod. 32 MB matches Claude's own API cap.
- **`parse_json_object`**: only *peeks*. Invalid JSON becomes `{}` and is still forwarded, so the provider
  returns its own proper 400. The gateway never rebuilds the body, so new API fields pass through with no
  gateway change.
- **`time.perf_counter()` for latency**: a monotonic clock (only moves forward). The wall clock can jump
  when the machine syncs time, which would produce negative or huge latencies.
- **`/health`**: liveness only, **no database call**. If a DB blip failed the health check, Kubernetes would
  restart every gateway copy at once and turn a small outage into a total one.

---

## 6. Auth ([gateway/auth.py](../gateway/auth.py))

**Problem:** know which tenant is calling, without storing anything that would let an attacker call as them
if the database leaked.

**Key idea: the key is proof, the tenant id is identity.** The key is checked once at the front door and
turned into `tenant.id`. Everything after (limits, metering) only uses the id. Keys can be rotated;
history (usage rows) never points at a key.

### Key format and generation

```python
KEY_PREFIX = "gw_"
generate_key() = "gw_" + secrets.token_urlsafe(32)     # 32 random bytes = 256 bits
```

- `secrets` (not `random`): cryptographically secure randomness.
- The `gw_` prefix makes keys recognisable by eye and by secret scanners (e.g. GitHub push protection).

### Storage: SHA-256, not bcrypt

```python
hash_key(key) = sha256(key).hexdigest()
```

Only the hash is stored (`tenants.api_key_hash`, `UNIQUE`, which also creates the lookup index).

**Why a fast hash is safe here:** bcrypt/argon2 exist to slow down guessing of *human-chosen* passwords,
which have little randomness. Our keys have 256 bits of randomness; guessing is hopeless no matter how fast
the hash is. And a fast, **unsalted, deterministic** hash is what makes lookup possible at all: same key →
same hash → one indexed `WHERE api_key_hash = $1` query. bcrypt is salted (same key → different hash each
time), so you can't look a key up by its hash, and it would add ~100 ms per request.

**When this would be wrong:** if tenants chose their own keys. Then we'd need a slow hash and a key id
prefix to find the row first.

### Lookup

```sql
SELECT id, burst, refill_per_second, monthly_token_quota
FROM tenants WHERE api_key_hash = $1
```

One query returns identity **and** limits, so adding limits in Project 2 cost no extra round trip.

### Behaviour

| Situation | Response | Why |
|---|---|---|
| No key, or wrong key | `401 authentication_error "invalid x-api-key"` | Same message for both, so an attacker can't probe which keys exist. |
| Postgres down | `503 api_error "auth database unavailable"` | **Fail closed**: if we can't check, nobody gets in. 503 (not 401) tells the caller it's temporary and retryable. |

The header is `x-api-key` because that's what the Anthropic SDK already sends.

### Creating a tenant ([gateway/create_tenant.py](../gateway/create_tenant.py))

```bash
uv run --env-file .env python -m gateway.create_tenant <name>
```

Generates a key, inserts `(name, hash)`, prints the key **once**. It is never stored in plain text, so a lost
key can only be replaced, not recovered. A duplicate name exits with an error (`UniqueViolationError`).

---

## 7. Limits ([gateway/limits.py](../gateway/limits.py))

**Problem:** stop a tenant calling too **fast** (rate limit) or too **much** (monthly token quota), before
any money is spent, and stay correct when several gateway copies handle the same tenant at the same moment.

**Why Redis and not a Python dict:** with 2+ gateway copies, a per-process dict gives each copy its own
limit, so a tenant with "20 requests" gets 20 per copy. Redis is one shared jar every copy draws from.

**Why Lua scripts:** Redis runs one command at a time, and a Lua script runs as a single uninterruptible
step. Doing "read → decide → write" as separate commands from Python lets two concurrent requests both read
"1 left" and both pass (a race condition). Inside one script, nothing can happen in between.

All keys start with `gw:` so they can't collide with another app sharing the same Redis.

### 7.1 Rate limit: token bucket ("ticket jar")

Each tenant has a jar holding up to `burst` tickets. Each request takes one ticket. Tickets refill at
`refill_per_second`. So `burst` = how many requests can arrive at once, and `refill_per_second` = the
long-run average rate.

- Redis key: `gw:bucket:{tenant_id}`, a hash with fields `tickets` and `ts` (last update time).
- Defaults (from `004_tenant_limits.sql`): `burst = 20`, `refill_per_second = 1`.

The Lua script (`TOKEN_BUCKET`), in steps:

1. Get the current time from **Redis's clock** (`TIME`), not the gateway's. Clocks on different gateway
   copies drift; one clock for everyone keeps the math consistent.
2. Read `tickets` and `ts`. A missing key means a new (or expired) tenant: **full jar**.
3. Refill: `tickets = min(capacity, tickets + elapsed × rate)`. `elapsed = max(0, now − ts)`, so if the
   clock ever jumps backwards we treat it as zero time passed, instead of taking tickets away.
4. If `tickets ≥ 1`: take one, `wait = 0`. Otherwise `wait = (1 − tickets) / rate` (seconds until one
   whole ticket exists).
5. Save `tickets` and `ts`, and set `EXPIRE ceil(capacity / rate) + 1`. After that long, the jar would be
   full anyway, so the key can just disappear (no memory used by idle tenants).
6. Return `wait` **as a string**, because Redis converts Lua numbers to integers and would chop off the
   fraction.

Python turns `wait` into a whole number of seconds, **rounded up** (`math.ceil`), for the `retry-after`
header, so a caller that waits exactly that long is guaranteed to find a ticket.

**Worked example** (burst 20, refill 1/s): 25 requests arrive together. The first 20 take tickets. Requests
21–25 find ~0 tickets and get `429 rate_limit_error` with `retry-after: 1`. One second later there is one
ticket again.

Note that a refused request does **not** push the jar negative: it only records the refill. So hammering the
gateway while limited doesn't dig a deeper hole.

### 7.2 Monthly token quota: the "card hold"

Each tenant has `monthly_token_quota` tokens (input + output) per calendar month, UTC. Default 1,000,000.

- Redis key: `gw:quota:{tenant_id}:{YYYY-MM}`, a plain integer counter.
- **Reset = change the key name.** A new month produces a new key; nothing has to run at midnight. Old keys
  expire after 40 days (`QUOTA_KEY_TTL`), longer than any month.

**The race it solves.** The first version was "check the counter before, add the real tokens after". Twenty
simultaneous requests all read the same old count and all passed, overshooting by up to 20 calls. The fix
works like a hotel putting a hold on your card at check-in:

1. **Reserve** (before forwarding): hold the *estimated* tokens on the counter immediately, so a request
   arriving a millisecond later already sees them as used.
2. **Settle** (after the reply): swap the hold for the real count.

**Estimate** (`estimate_tokens`):

```
input  ≈ ceil(body_bytes / 4)                       # ~4 chars per token, whole body incl. JSON syntax → slight overestimate
output = 500 (TYPICAL_OUTPUT_TOKENS), or max_tokens if the caller asked for less
```

Output is a *typical* answer, not `max_tokens`. Holding `max_tokens` would refuse a tenant with 1,000 tokens
left just for asking `max_tokens: 4000`.

**The `RESERVE` script:**

```lua
used = GET key
if not used          → return 'missing'       -- Python rebuilds from Postgres, then retries once
if used >= quota     → return -1              -- over quota
hold = min(estimate, quota - used)            -- never hold more than what's left
INCRBY key hold
return hold
```

Holding "at most what's left" means a big estimate (say, a large image) takes the remaining allowance rather
than being refused while the tenant still has some left.

**Worked example:** quota 1,000,000, used 999,000, each request estimated at 600.

| Request | Counter before | Hold | Counter after | Result |
|---|---|---|---|---|
| A | 999,000 | 600 | 999,600 | allowed |
| B (same instant) | 999,600 | 400 (all that's left) | 1,000,000 | allowed |
| C (same instant) | 1,000,000 | — | 1,000,000 | `429 "monthly token quota exceeded"` |
| A settles, real = 300 | 1,000,000 | add 300 − 600 = −300 | 999,700 | — |

The quota-exceeded 429 carries `retry-after` = seconds until the start of next month (UTC), computed from
`started_at`.

**Settle (`ADD_IF_EXISTS`):** adds `actual − hold` (negative when the call used less than held), **only if
the counter still exists**. If Redis lost the counter meanwhile, the next rebuild reads this call's finished
row from Postgres, which already has the real tokens; adding to a fresh counter would count it twice.
`settle` skips the Redis call entirely when `actual == hold`, and `actual = 0` gives the whole hold back
(used when `metering.start` fails, so the call never happened).

**Settle never fails the caller.** It runs after the money is spent. A Redis error is logged; the counter is
just off until it's next rebuilt.

### 7.3 Rebuilding a lost counter from Postgres

Redis has no disk (on purpose). If it restarts, every quota counter is gone. Without a rebuild, every tenant's
quota would silently reset to zero. So when `RESERVE` returns `'missing'`, `_rebuild` recounts from
`usage_events`, the source of truth:

```sql
SELECT coalesce(sum(CASE
         WHEN status_code IS NULL THEN coalesce(quota_hold, 0)            -- still in flight: count its hold
         ELSE coalesce(input_tokens, 0) + coalesce(output_tokens, 0)       -- finished: count real tokens
       END), 0)
FROM usage_events
WHERE tenant_id = $1 AND created_at >= <month start> AND created_at < <next month start>
```

This produces **exactly what the live counter would hold**: real tokens for finished calls, holds for calls
in flight. That's why `quota_hold` is stored on the usage row (migration 005). A call that crashed and never
finished keeps counting its hold forever, which errs on the safe side.

The result is written with **`SET … NX EX 40d`**. `NX` = "only if it doesn't exist": if two requests both
see `'missing'` and both rebuild, the first one wins and the second doesn't overwrite a counter that may
already have new holds on it.

`reserve` tries twice: first attempt may see `'missing'`, rebuild, second attempt succeeds. If the counter
vanishes again right after the rebuild, it raises (→ 503).

### 7.4 One start time for the month

`started_at = datetime.now(timezone.utc)` is taken **once** per call in `main.py`, and the same value:

- picks the Redis quota key (`{YYYY-MM}`),
- is stored as `usage_events.created_at` (instead of Postgres `now()`),
- is used by `settle`.

Without this, a call starting at 23:59:59 on the 31st and finishing after midnight could hold tokens in one
month's counter and settle in the next, or be counted in a different month by Postgres than by Redis.

Month math: `next_month_start` takes the 28th of the current month (exists in every month), adds 4 days
(always lands in the next month), then snaps to day 1.

### 7.5 Failure behaviour

`LIMIT_ERRORS = (RedisError, OSError, PostgresError, InterfaceError)`. Any of them during `rate_limit` or
`reserve` → `503 "rate limiter unavailable"`. **Fail closed**: without working limits a runaway tenant could
spend without bound. The Redis client has **1-second** socket and connect timeouts: every request waits on
Redis, so a hung Redis must fail fast rather than make every caller hang.

**Tradeoff:** Redis down = the whole gateway refuses traffic. The alternative (fail open) keeps serving but
removes all spending protection. For a gateway that spends real money per request, closed was the call.

---

## 8. Forwarding ([gateway/forwarding.py](../gateway/forwarding.py))

**Problem:** send the caller's request to the provider and return the answer, without the gateway needing a
code change every time the provider's API gains a field.

**Design: a pure pass-through.** The body bytes are forwarded exactly as received. The gateway only *peeks*
at a few fields in `main.py`; it never parses and rebuilds the request.

### Timeouts

```python
TIMEOUT = httpx.Timeout(120.0, connect=5.0)
```

- **Connect: 5 s.** A dead upstream should fail fast.
- **Read: 120 s.** httpx's default of 5 s would cut off long LLM answers that we're still paying for. Note
  this is a timeout *between* reads, not on the whole call: a stream that keeps sending chunks can run longer.

### Header allowlists, both directions

**Request (caller → upstream):** built from scratch, then a short allowlist copied over:

| Header | Value |
|---|---|
| `authorization` | `Bearer {UPSTREAM_API_KEY}`: **the gateway's** provider key (OpenRouter uses Bearer) |
| `content-type` | `application/json` |
| `anthropic-version` | `2023-06-01` by default; the caller's value wins if sent |
| `anthropic-beta` | copied if the caller sent it |

Why an allowlist and not "copy everything": the caller's own credentials are a *gateway* key and must never
reach the provider, and hop-level headers like `Host` describe the caller's connection, not ours. The default
`anthropic-version` means switching to Anthropic direct (which requires it) won't break callers that omit it.

**Response (upstream → caller):** only `retry-after`, `request-id`, and anything starting with
`anthropic-ratelimit-`. `retry-after` lets the caller's SDK back off correctly on a provider 429;
`request-id` is what provider support asks for. **Not copied:** `content-encoding` and `content-length`,
because httpx has already decompressed the body, so they'd describe bytes the caller never receives.

### Two ways to call

- `forward_messages()`: `client.post(...)`, waits for the whole reply.
- `open_stream()`: `client.send(request, stream=True)`, returns as soon as status + headers arrive; the body
  is read chunk by chunk later. **The caller must `aclose()` it**, which is also what tells the provider to
  stop generating.

### Upstream errors

Upstream HTTP errors (400 bad model, 429 provider rate limit, 500…) are **passed through unchanged**, status
and body: they're information the caller needs. Only transport failures are translated:

- `httpx.TimeoutException` → `504 "upstream timed out"` (slow)
- `httpx.RequestError` (DNS, refused, reset) → `502 "upstream unreachable"` (down)

Separate codes so callers, and later dashboards, can tell slow from down.

### Switching to Anthropic direct

Change `UPSTREAM_BASE_URL` to `https://api.anthropic.com`, the key in `UPSTREAM_API_KEY`, and the one line
that builds `authorization: Bearer …` to `x-api-key: …`. Model names also change (`claude-haiku-4-5` instead
of `anthropic/claude-haiku-4.5`), which affects the price table.

---

## 9. Metering ([gateway/metering.py](../gateway/metering.py))

**Problem:** record who called, how many tokens, what it cost, and how long it took, for every call, even
when things break halfway.

### Two writes per call

```
start()   INSERT usage_events (tenant_id, model, created_at, quota_hold)   ← BEFORE forwarding
finish()  UPDATE usage_events SET status_code, tokens, cost, latency, outcome WHERE id = …   ← AFTER
```

| Write | Runs when | If it fails | Why |
|---|---|---|---|
| `start` | Nothing spent yet | **Refuse** (503), give the quota hold back | Better to refuse than let through a call we'd have no record of. |
| `finish` | Money already spent | **Log it, return the answer anyway** | Failing would make the caller retry and pay twice. |

If `finish` fails (or the pod crashes mid-call), the row from `start` stays with `status_code IS NULL`. The
call is never invisible: it shows up as "started, never finished". `finish` returns the call's total tokens
for `settle`.

### Reading usage from a buffered reply (`read_usage`)

- Only a `200` reply carries usage. Errors get **NULL tokens, not 0**: "there was no reply" and "a reply
  that used 0 tokens" are different facts.
- Reads `usage.input_tokens` and `usage.output_tokens` from the JSON body.
- OpenRouter also returns `usage.cost` (what it charged). Stored as `provider_cost_usd`. Converted via
  `Decimal(str(x))` so the float's binary noise doesn't leak into the Decimal.

### Cost (`compute_cost`)

```python
PRICES_PER_MTOK = {"anthropic/claude-haiku-4.5": (Decimal("1.00"), Decimal("5.00"))}   # USD per million (in, out)
cost = (input_tokens × in_price + output_tokens × out_price) / 1,000,000
```

- A model not in the table gets `cost_usd = NULL`, never a fake 0, so `WHERE cost_usd IS NULL` finds models
  that need a price.
- Cost is stored **at call time**, so a future price change doesn't rewrite history.
- **Reconciliation:** comparing our `cost_usd` with the provider's `provider_cost_usd` catches a stale price
  table.

### The `outcome` column

| Value | Meaning | Token counts |
|---|---|---|
| `complete` | Reply fully read | Exact (or NULL if there was no successful reply, e.g. a 400) |
| `client_disconnected` | Caller left mid-stream | `output_tokens` estimated |
| `upstream_error` | Stream broke on the provider's side | `output_tokens` estimated |
| NULL | Row older than migration 003, or the call never finished | — |

---

## 10. Streaming ([gateway/streaming.py](../gateway/streaming.py))

**Problem:** with `"stream": true`, the reply arrives as a series of **Server-Sent Events** (SSE: a
long-lived HTTP response where the server writes `event:` / `data:` lines as they happen). The caller must
see words as they arrive, *and* we must still meter the call, including when it ends badly.

The first version of the gateway let streamed calls through **unmetered** (200 with NULL tokens). This module
is the fix.

### Where the tokens are in a stream

```
event: message_start        data: {"message": {"usage": {"input_tokens": 25, "output_tokens": 1}}}   ← input count, first
event: content_block_delta  data: {"delta": {"text": "Hello"}}                                        ← the words
event: content_block_delta  data: {"delta": {"text": " world"}}
event: message_delta        data: {"usage": {"output_tokens": 12, "cost": ...}}                        ← final output count
event: message_stop                                                                                    ← clean end
```

### `StreamUsage`: reading the counts as bytes pass by

- **`feed(chunk)`**: network chunks don't line up with events; one event can be split across two chunks. It
  keeps the unfinished last line (`_partial_line`) until the rest arrives, then parses complete `data:` lines.
- **`message_start`** → `input_tokens`. **`content_block_delta`** → adds the length of `text`,
  `partial_json` (tool calls) or `thinking` to `chars_delivered`. **`message_delta`** → exact
  `output_tokens`, sets `final_usage_seen`; newer API versions repeat `input_tokens` here, taken if present.
  **`message_stop`** → `stopped`. **`error`** event → `errored`.
- Any unreadable event is ignored: metering must never break the caller's stream.
- **`usage()`**: if the final count arrived, return it. If not, **estimate** output as
  `ceil(chars_delivered / 4)`. Recording NULL instead would let a tenant get output tokens free by
  disconnecting just before the end. Estimate + an `outcome` flag beats NULL.

The bytes themselves go to the caller **unchanged**; the gateway only looks.

### `MeteredStream`: guaranteed cleanup on any ending

`MeteredStream` subclasses Starlette's `StreamingResponse`.

- **`_relay()`**: `async for chunk in upstream.aiter_bytes(): meter.feed(chunk); yield chunk`. If the
  provider's connection breaks (`httpx.HTTPError`), it sets `upstream_failed` and ends. Once `200` has gone
  out, there's no clean way to send an HTTP error; the caller just sees the stream end early. Reaching the end
  sets `upstream_done`.
- **`__call__`** wraps the whole response in `try / finally`. The `finally` runs however the stream ended,
  and does, in order:
  1. `upstream.aclose()`: closing the upstream connection is what **tells the provider to stop
     generating**. A caller who leaves stops costing money.
  2. `metering.finish(...)` with the counts from `StreamUsage` and the outcome.
  3. `limiter.settle(...)`: estimated tokens count against the quota too, so disconnecting isn't a way
     around the quota any more than around billing.

**Why the cleanup is inside `anyio.CancelScope(shield=True)`:** when the caller disconnects, the server
*cancels* the request's task. A cancelled task gets cancelled again at its next `await`, so a plain
`await metering.finish(...)` in the `finally` would itself be cancelled and the row would never be written.
The shield lets the cleanup run to completion.

**Outcome logic (`_outcome`):**

```
upstream_failed or an SSE error event     → upstream_error
reached end of upstream AND saw message_stop → complete
reached end of upstream, no message_stop  → upstream_error
never reached the end                     → client_disconnected
```

The last rule works because Starlette (with uvicorn's ASGI 2.3) handles a client disconnect by quietly
cancelling the relay, not by raising an exception we could catch. "We never got to the end" is how the
disconnect is detected.

**A streaming request that isn't a stream:** `is_event_stream()` requires `200` **and**
`content-type: text/event-stream`. A bad model name gets a plain JSON 400 sent up front, even with
`stream: true`. In that case `main.py` reads it whole (`aread()`) and handles it exactly like a buffered
reply.

---

## 11. Data model

Migrations live in [db/](../db/) and are applied in name order. Locally with `psql "$DATABASE_URL" -f …`; on
Kubernetes, Postgres runs them automatically on first start with an empty data folder (see section 16).

### `tenants`

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint` identity, PK | The tenant's identity. Never changes. |
| `name` | `text NOT NULL UNIQUE` | Human label. |
| `api_key_hash` | `text NOT NULL UNIQUE` | SHA-256 hex. `UNIQUE` also creates the index for the per-request lookup. |
| `created_at` | `timestamptz` default `now()` | |
| `burst` | `int NOT NULL DEFAULT 20 CHECK (> 0)` | Token bucket size (004). |
| `refill_per_second` | `double precision NOT NULL DEFAULT 1 CHECK (> 0)` | Bucket refill rate (004). |
| `monthly_token_quota` | `bigint NOT NULL DEFAULT 1000000 CHECK (>= 0)` | Input + output tokens per UTC month (004). |

`CHECK` constraints put validation in the database: a `refill_per_second` of 0 would divide by zero in the
Lua script (`capacity / rate`), and a `burst` of 0 would mean no request ever passes, so neither can be stored
at all.

### `usage_events`

| Column | Type | Written by | Notes |
|---|---|---|---|
| `id` | `bigint` identity, PK | start | |
| `tenant_id` | `bigint NOT NULL` FK → `tenants(id)` | start | The id, never the key: keys rotate, history must not. |
| `model` | `text` | start | As sent by the caller. NULL if missing/invalid. |
| `created_at` | `timestamptz NOT NULL` | start | The gateway's `started_at`, not DB `now()` (see 7.4). |
| `quota_hold` | `int` | start | The estimated tokens held (005). Used by the rebuild. |
| `status_code` | `int` | finish | **NULL = started, never finished.** |
| `input_tokens` | `int` | finish | NULL when there was no successful reply. |
| `output_tokens` | `int` | finish | Estimated when `outcome` isn't `complete`. |
| `cost_usd` | `numeric(12, 8)` | finish | Our price table. NULL = unknown model. |
| `provider_cost_usd` | `numeric(12, 8)` | finish | What the provider said it charged, if it told us. |
| `latency_ms` | `int` | finish | Forward start → reply end (monotonic clock). |
| `outcome` | `text` | finish | See section 9 (003). |

**Index:** `(tenant_id, created_at)`, exactly the shape of "how much did tenant X use this month?", which is
both the quota rebuild query and the obvious billing query.

`numeric(12, 8)`: up to 9,999.99999999 USD per row with 8 decimal places, enough for sub-cent per-call costs.

### Useful queries

```sql
-- Calls that started but never finished (crash, or the final write failed)
SELECT * FROM usage_events WHERE status_code IS NULL;

-- Models missing from the price table
SELECT DISTINCT model FROM usage_events WHERE cost_usd IS NULL AND status_code = 200;

-- Reconciliation: our cost vs the provider's
SELECT model, sum(cost_usd) AS ours, sum(provider_cost_usd) AS theirs
FROM usage_events WHERE created_at >= date_trunc('month', now()) GROUP BY model;

-- This month's usage per tenant
SELECT t.name, sum(coalesce(input_tokens,0) + coalesce(output_tokens,0)) AS tokens, sum(cost_usd) AS usd
FROM usage_events u JOIN tenants t ON t.id = u.tenant_id
WHERE u.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') GROUP BY t.name;
```

---

## 12. Failure handling: fail open vs fail closed

**Fail closed** = refuse the request when a dependency is broken. **Fail open** = let it through anyway. The
rule used everywhere in this codebase:

> **Before money is spent → fail closed. After money is spent → fail open.**

| Failure | When | Behaviour | Reasoning |
|---|---|---|---|
| Postgres down during auth | Before | 503, refuse | Can't verify the caller. |
| Redis down during rate limit / reserve | Before | 503, refuse | No limits = unbounded spend. |
| Counter missing in Redis | Before | Rebuild from Postgres, retry | Redis restart must not reset quotas to 0. |
| Postgres down during `metering.start` | Before | Give hold back, 503 | No record = invisible spend. |
| Upstream timeout / unreachable | During | 504 / 502, metered, hold settled to 0 tokens | The caller needs to know; we record the failure. |
| Upstream returns an error | During | Passed through, metered | The error is information for the caller. |
| Stream breaks on provider side | During | Stream ends early, `outcome = upstream_error`, tokens estimated | No way to send an HTTP error after 200. |
| Caller disconnects mid-stream | During | Upstream closed (stops generation), `client_disconnected`, tokens estimated | Stop the spend; bill what was delivered. |
| Postgres down during `metering.finish` | After | Logged, answer returned, row stays `status_code NULL` | Failing would cause a paid retry. |
| Redis down during `settle` | After | Logged, answer returned | Counter slightly off until next rebuild; Postgres has the truth. |
| Gateway pod crashes mid-call | Any | Row stays `status_code NULL` with its hold; rebuild keeps counting the hold | Errs on the safe side (tenant slightly under-served, never over). |

Errors are caught by **explicit lists** (`DB_ERRORS`, `LIMIT_ERRORS`), never `except Exception`, so a real
bug in our own code still surfaces as a 500 instead of hiding behind "database unavailable".

---

## 13. Error contract

All gateway-generated errors use **Anthropic's error shape**, so callers built on the Anthropic SDK parse
gateway errors exactly like Claude's (and the SDK raises the matching exception class):

```json
{"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit exceeded"}}
```

| Status | `error.type` | Message | Extra header | Retryable |
|---|---|---|---|---|
| 401 | `authentication_error` | invalid x-api-key | | no |
| 413 | `request_too_large` | request body exceeds 32 MB | | no |
| 429 | `rate_limit_error` | rate limit exceeded | `retry-after: <s>` (seconds to next ticket) | yes |
| 429 | `rate_limit_error` | monthly token quota exceeded | `retry-after: <s>` (seconds to next month) | at month start |
| 502 | `api_error` | upstream unreachable | | yes |
| 503 | `api_error` | auth database unavailable | | yes |
| 503 | `api_error` | rate limiter unavailable | | yes |
| 503 | `api_error` | usage database unavailable | | yes |
| 504 | `api_error` | upstream timed out | | yes |
| any | (provider's) | (provider's) | provider headers from the allowlist | depends |

---

## 14. Configuration

All config is environment variables. No config files, nothing baked into the image.

| Variable | Used by | Example | Secret? |
|---|---|---|---|
| `UPSTREAM_BASE_URL` | forwarding | `https://openrouter.ai/api` | no (ConfigMap) |
| `UPSTREAM_API_KEY` | forwarding | OpenRouter key | **yes** (Secret) |
| `DATABASE_URL` | main, create_tenant | `postgresql://gateway:<pw>@postgres:5432/gateway` | **yes** (contains the password) |
| `REDIS_URL` | limits | `redis://redis:6379/0` | no (ConfigMap) |
| `GATEWAY_API_KEY` | scripts/client.py only | a `gw_…` tenant key | yes (local only) |

Locally these come from `.env` (git-ignored, docker-ignored) via `uv run --env-file .env`. On Kubernetes,
non-secrets come from the `gateway-config` ConfigMap and secrets from the `gateway-secrets` Secret, which is
created by hand and never committed.

**Tunable constants in code** (deliberately not env vars: they don't change between environments):

| Constant | Where | Value |
|---|---|---|
| `MAX_BODY_BYTES` | main.py | 32 MB |
| `TIMEOUT` | forwarding.py | 120 s read / 5 s connect |
| `DEFAULT_ANTHROPIC_VERSION` | forwarding.py | `2023-06-01` |
| `TYPICAL_OUTPUT_TOKENS` | limits.py | 500 |
| `QUOTA_KEY_TTL` | limits.py | 40 days |
| Redis socket timeouts | limits.py | 1 s |
| `PRICES_PER_MTOK` | metering.py | Haiku 4.5: $1 in / $5 out per million |
| `CHARS_PER_TOKEN` | metering.py | 4 |

Per-tenant limits (`burst`, `refill_per_second`, `monthly_token_quota`) live in the `tenants` table and take
effect on the tenant's next request (they're read on every auth lookup; there's no cache).

---

## 15. Packaging: the container image

[Dockerfile](../Dockerfile), two stages ("a workshop and a delivery box"):

**Stage 1 (build):** `python:3.12-slim` + pinned `uv 0.11.7`.
- Copies only `pyproject.toml` and `uv.lock` first, then `uv sync --frozen --no-dev --no-install-project`.
  Docker caches this layer, so editing gateway code doesn't reinstall every dependency. `--frozen` fails the
  build if the lock file is out of date instead of silently resolving new versions.
- `UV_COMPILE_BYTECODE=1`: compile `.pyc` at build time, not on every pod cold start.

**Stage 2 (runtime):** fresh `python:3.12-slim`, copies only `/app/.venv` and `gateway/`.
- No uv, no build tools: smaller pull and fewer packages for a vulnerability scanner to flag.
- Runs as **non-root UID 10001**. A fixed numeric UID lets Kubernetes enforce `runAsNonRoot`.
- `PYTHONUNBUFFERED=1` so logs appear immediately in `kubectl logs`.
- `CMD uvicorn gateway.main:app --host 0.0.0.0 --port 8000` (no `--reload`, that's for local dev).

[.dockerignore](../.dockerignore) keeps `.env` (the real API key), `.venv`, `.git`, `infra`, and `*.tfvars` out
of the build context, so secrets can never end up in a pushed image.

---

## 16. Running on Kubernetes (minikube + Argo CD)

Full detail, diagrams and under-the-hood walkthrough: **[deployment.md](deployment.md)**. Summary:

**Where it runs today:** MacBook → Colima VM (Linux, Docker engine, 4 CPU / 6 GB) → minikube (one
Kubernetes node as a container) → namespace `llm-gateway`. Cost: $0.

### What's in `k8s/`

| File | Objects | Key decisions |
|---|---|---|
| `00-namespace.yaml` | Namespace `llm-gateway` | `00-` prefix so `kubectl apply -f k8s/` creates it first. Deleting it removes everything. |
| `config.yaml` | ConfigMap `gateway-config` | `UPSTREAM_BASE_URL`, `REDIS_URL=redis://redis:6379/0` ("redis" = the Service name, resolved by cluster DNS). |
| `gateway.yaml` | **Rollout** (4 replicas) + Service `gateway` (all pods) + Service `gateway-canary` (canary pods only) | `runAsNonRoot`, `allowPrivilegeEscalation: false`; only the 2 needed Secret keys injected; startup probe (60 s to boot) then liveness + readiness on `/health`, 3 s timeouts; requests 100m CPU / 128Mi, limit 256Mi. Image `ghcr.io/…/gateway:<git-sha>`, written by CI, pulled with the `ghcr-pull` Secret, `IfNotPresent` (safe because tags are never reused). Canary: `maxUnavailable: 0`, `maxSurge: 1`. |
| `gateway-smoke.yaml` | AnalysisTemplate `gateway-smoke` | A Job sends 5 fake-key requests to `gateway-canary`; all must return 401 (proves the new code runs and reaches Postgres, $0). Own file because CI rewrites every `image:` line in `gateway.yaml`. |
| `postgres.yaml` | PVC (1Gi) + Deployment + Service | `postgres:17-alpine` (major pinned: a new major can't read the old data folder); `strategy: Recreate` (two Postgres processes on one data folder corrupts it); `PGDATA` in a subfolder (avoids `lost+found`); `pg_isready` readiness with a 5 s timeout (the 1 s default flapped under load). |
| `redis.yaml` | Deployment + Service | `redis:8-alpine`, **no disk and snapshots off** (`--save "" --appendonly no`): every counter is rebuildable from Postgres. |
| `db-init.yaml` | ConfigMap `db-init` | **Generated** from `db/*.sql`, mounted at `/docker-entrypoint-initdb.d`. Postgres runs those files once, on first start with an empty data folder. |

**Why 4 gateway replicas:** with no traffic router, the canary split *is* the pod count (the Service picks
pods at random), so 4 pods allow clean 25% / 50% steps. Safe because the gateway is stateless.

### Argo CD (GitOps)

[argocd/gateway.yaml](../argocd/gateway.yaml) is an Argo CD `Application`: "keep namespace `llm-gateway` on this
cluster identical to `k8s/` on `main`". Applied once by hand; after that, a push to `main` *is* the deploy.

- Source: `git@github.com:musishere/AI-platform-engineering.git`, path `k8s`, via a read-only SSH deploy key
  stored in a Secret in the `argocd` namespace.
- `automated.prune: true`: a file deleted from `k8s/` has its object deleted from the cluster. Only objects
  Argo CD created, so the hand-made `gateway-secrets` is safe. **Risk:** deleting `postgres.yaml` deletes the
  database.
- `automated.selfHeal: true`: hand edits (`kubectl edit`) are reverted to what Git says. Git stays the single
  source of truth. Consequence: rollback is `git revert`, not `kubectl rollout undo`.
- **Pull-based:** Argo CD runs inside the cluster and pulls from Git, so CI will never need cluster
  credentials.

### CI/CD and canary releases

```
git push (code) → GitHub Actions: build → Trivy scan → push ghcr.io/…/gateway:<sha> → commit tag to k8s/
                → Argo CD: applies the new Rollout spec
                → Argo Rollouts: 25% (1 of 4 pods) → smoke test → 2 min → 50% → 2 min → 100%
                                 smoke test fails → abort: canary removed, stable back to 4, app Degraded
```

- **CI** ([.github/workflows/gateway.yml](../.github/workflows/gateway.yml)) runs only when image inputs change
  (`gateway/**`, `Dockerfile`, lock files). Scan happens before push, and the exact scanned image is pushed.
  The tag commit doesn't retrigger CI (GitHub never starts workflows from `GITHUB_TOKEN` commits).
- **Argo CD** never builds anything; the tag commit in `k8s/` is the only link between code and cluster.
- **Argo Rollouts** turns `setWeight` into pod counts (`ceil(replicas × weight)` for each side); kube-proxy
  spreads *connections* evenly over ready pods, so the split is per connection, not per request.
- **Rollback after an abort** is `git revert` + push. Hand edits get reverted by selfHeal.
- **Known gap:** the smoke test runs once, at 25%. Errors that only appear on real traffic during the pauses
  aren't caught; Project 4 adds a metrics-based background analysis.

---

## 17. Cloud infrastructure (Terraform on Oracle Cloud)

**Cloud:** Oracle Cloud (OCI), home region Mumbai (`ap-mumbai-1`), chosen over AWS for its Always Free tier.
Terraform is split into **two root modules** with separate state, on purpose.

### `infra/bootstrap/`: permanent and free

| Resource | Why |
|---|---|
| Compartment `ai-platform` | One folder for everything the project creates; nothing mixes with the rest of the account. |
| Object Storage bucket `ai-platform-tfstate` | Terraform's remote state for every part of the project. **Private**, **versioned** (a damaged state file can be rolled back), `prevent_destroy = true` (deleting it would orphan every resource it tracks). |
| Budget `monthly-safety-net` ($50) over the **whole account** (root compartment, not just the project) | Nothing created elsewhere can slip past the alerts. |
| Alert rules at **$20** and **$50** actual spend | Email warnings, with the action to take in the message. |

**Why separate from the cluster:** `terraform destroy` in `infra/cluster` runs every session. It must never be
able to delete the two things that protect us: the state bucket and the budget alerts.

**Chicken-and-egg:** the bucket didn't exist on the first apply, so bootstrap's first run used local state;
then `terraform init -migrate-state` moved it into the bucket. The OCI backend **locks** the state during a
run, so two runs at once can't corrupt it.

**Auth:** `auth = "SecurityToken"`: a short-lived browser login (`oci session authenticate`, ~1 hour)
instead of a permanent API key file. A leaked session dies within the hour; a leaked key works until someone
notices.

### `infra/cluster/`: built and destroyed each session

Reads the compartment id from bootstrap's state via `terraform_remote_state` (no copy-pasted ids, so the two
can't drift).

**Network ([network.tf](../infra/cluster/network.tf)), following Oracle's reference layout for OKE with flannel:**

```
VCN 10.0.0.0/16 ("the building")
├── subnet-api-endpoint   10.0.0.0/28    PUBLIC    Kubernetes API endpoint (kubectl from a laptop)
├── subnet-workers        10.0.10.0/24   PRIVATE   worker nodes: gateway, Postgres, Redis
│                                                  prohibit_public_ip_on_vnic = true
└── subnet-load-balancers 10.0.20.0/24   PUBLIC    where OCI creates LBs for Services (front door)

Gateways ("doors")
├── Internet gateway   two-way, only for the public subnets
├── NAT gateway        out-only: workers can pull images / call OpenRouter; nothing can connect in
└── Service gateway    private path to Oracle services (Object Storage, registry, OKE) without the internet

Route tables
├── public   0.0.0.0/0 → internet gateway
└── private  0.0.0.0/0 → NAT;  Oracle services CIDR → service gateway
```

**Security lists** (stateful firewall rules per subnet: if a connection is allowed, its replies are allowed
automatically):

- **API subnet:** in from workers on 6443 (Kubernetes API) and 12250 (control plane); in from anywhere on 6443
  (kubectl, still requires an OCI login; "reachable is not usable"); ICMP type 3 code 4 ("packet too big", for
  path MTU discovery; without it some connections hang silently).
- **Workers:** all traffic between workers (pod-to-pod across nodes); TCP in from the API subnet; in from the LB
  subnet on NodePorts 30000–32767 and kube-proxy health 10256; out to the internet via NAT.
- **Load balancers:** **no ingress yet**, on purpose. The listener port is added only when the gateway is
  actually exposed. Until then, nothing can come in the front door.

**Tradeoff (public API endpoint):** much simpler than a private endpoint + bastion host, at the cost of a
slightly larger attack surface (still authenticated).

**Cluster ([cluster.tf](../infra/cluster/cluster.tf)):**

- OKE `BASIC_CLUSTER` (free control plane), Kubernetes **v1.36.1** pinned (a rebuild next week gives the same
  cluster; upgrades are a deliberate one-line change). Will switch to `ENHANCED_CLUSTER` for Workload
  Identity.
- `FLANNEL_OVERLAY` networking: pods get addresses from `10.244.0.0/16`, Services from `10.96.0.0/16`, so pods
  don't consume VCN addresses. Must not overlap the VCN range.
- Node pool `workers`: **2 × `VM.Standard.A1.Flex` (ARM), 1 OCPU + 6 GB each, 50 GB boot**: exactly the
  Always Free allowance, split into two machines so one can die while the other keeps running.
- Node image chosen **by filter**, not hard-coded: newest `Oracle-Linux-9.x-aarch64-…-OKE-1.36.1-` image, so
  each rebuild picks up Oracle's latest patched image for the same Kubernetes version.

**Current status:** the network applies cleanly; the node pool never started because Mumbai reports
`OUT_OF_HOST_CAPACITY` for free ARM (and even the free AMD micro). The OKE deploy is paused until funded;
Argo CD and CI/CD are being built on minikube meanwhile. Moving later = pointing Argo CD at the OKE cluster.

---

## 18. How to run things

### Locally (no Kubernetes)

```bash
# Postgres + Redis running (via docker), .env filled in, then:
for f in db/*.sql; do psql "$DATABASE_URL" -f "$f"; done       # schema
uv run --env-file .env python -m gateway.create_tenant myapp    # prints gw_… key once
uv run --env-file .env uvicorn gateway.main:app --reload        # gateway on :8000
uv run --env-file .env python scripts/client.py                 # end-to-end test (needs GATEWAY_API_KEY)
```

[scripts/client.py](../scripts/client.py) uses the **real Anthropic SDK** with only `base_url` and `api_key`
changed (and `max_retries=0`, so every failure is visible and no retry adds an extra usage row). It checks:
a normal request, a streamed request, a wrong key (expects `AuthenticationError` 401), and a bad model (expects
`BadRequestError` 400). If it passes unchanged, the gateway is a true drop-in for the Claude API.

### On minikube

See [deployment.md §10 and §21](deployment.md) for the full command list. Short version:

```bash
colima start --cpu 4 --memory 6
minikube start --driver=docker --cpus=4 --memory=5g
docker build -t gateway:dev . && minikube image load gateway:dev
# create gateway-secrets from .env, install Argo CD, add the repo deploy key, then:
kubectl apply -f argocd/gateway.yaml
kubectl port-forward -n llm-gateway svc/gateway 8000:80
```

After changing `db/*.sql`, regenerate the init ConfigMap:
`kubectl create configmap db-init -n llm-gateway --from-file=db/ --dry-run=client -o yaml > k8s/db-init.yaml`

### Terraform

```bash
oci session authenticate --region ap-mumbai-1 --profile-name DEFAULT   # ~1 hour
terraform -chdir=infra/bootstrap plan                                   # rarely touched
terraform -chdir=infra/cluster apply
terraform -chdir=infra/cluster destroy                                  # END OF EVERY SESSION
```

---

## 19. Known limits and deliberate shortcuts

Marked in code with `ponytail:` comments where they apply. Each is a conscious "not yet", with the upgrade
path.

| Limit | Impact | Upgrade path |
|---|---|---|
| Prompt-cache tokens not priced (`compute_cost`) | `cost_usd` is low for tenants using caching; reconciliation shows the gap | Price `cache_creation` / `cache_read` tokens once a tenant uses caching. |
| Price table has one model | Other models get `cost_usd = NULL` (by design, findable) | Add rows as models are used. |
| Quota can slightly overshoot | If a call's real usage exceeds its hold, the counter can end above the quota; the *next* request is refused | Acceptable soft limit; a stricter version would hold `max_tokens`, at the cost of refusing legitimate calls. |
| Auth hits Postgres on every request | One DB round trip per call; limit changes apply instantly | A short TTL cache in Redis/memory if DB load matters. |
| Up to 32 MB body held in memory per request | Memory ≈ concurrent requests × body size | Stream the body upstream instead of buffering (the estimate would then need `Content-Length`). |
| 120 s timeout is per read, not per call | A slow but steady stream can run indefinitely | An overall deadline per call if needed. |
| After a streamed 200, errors can't be signalled over HTTP | Caller sees a truncated stream; the row records `upstream_error` | Inherent to SSE; could inject an SSE `error` event. |
| `/health` used for readiness too | A pod stays "ready" while Postgres is down | A DB-checking `/ready` for readiness once real traffic matters. |
| `k8s/db-init.yaml` is a copy of `db/` | Can drift from the real migrations | CI check, or a migration Job, when the next migration lands. |
| `db-init` only runs on an empty data folder | New migrations don't apply to an existing cluster DB | Same: a migration Job. |
| In-cluster Postgres, 1 replica, no backups | Pod restart = seconds of downtime; lost PVC = lost data | Managed database for real data. |
| Canary smoke test runs once, at 25% | A bug that shows only on real traffic during the pauses reaches 100% | Metrics-based background analysis (Project 4). |
| Canary split by pod count | Per connection, smallest step 25% with 4 pods | Ingress with traffic weights for exact or 1% steps. |
| ConfigMap changes don't restart pods | Env vars are read once at start | `rollout restart`, or hashed ConfigMap names (Kustomize). |

---

## 20. Key decisions (interview stories)

Each: what was chosen, what was given up, when the other option would be better.

1. **SHA-256 for API keys, not bcrypt.** Chose: fast, deterministic, indexable lookup. Gave up: protection
   against brute force, which 256-bit random keys don't need. Other option better when: users choose their
   own secrets.

2. **Pass-through forwarding.** Chose: forward the body untouched. Gave up: the ability to validate or
   rewrite requests centrally. Other option better when: guardrails need to inspect/redact content (a later
   project), and even then only for the fields they need.

3. **Two-phase metering (insert before, update after).** Chose: every call leaves a row, even crashes. Gave
   up: one extra DB write per call. Other option (one write at the end) better when: losing a small fraction
   of usage records is acceptable, e.g. analytics rather than billing.

4. **Fail closed before spending, fail open after.** Chose: refuse on dependency failure only while nothing
   is spent. Gave up: availability when Postgres or Redis is down. Other option better when: availability
   matters more than spend control (e.g. an internal tool with a fixed budget elsewhere).

5. **Estimate + flag instead of NULL for broken streams.** Chose: bill ~4 chars/token for what was delivered,
   marked `client_disconnected`. Gave up: exactness on those rows. Other option (NULL) would let tenants get
   output free by disconnecting.

6. **Redis counters with no persistence, rebuildable from Postgres.** Chose: one source of truth (Postgres),
   Redis as a disposable fast cache. Gave up: a brief rebuild query after a Redis restart. Other option
   (Redis with AOF/disk) better when: the counters *can't* be recomputed from elsewhere.

7. **Token bucket with Redis's clock, inside Lua.** Chose: atomic, drift-free rate limiting across copies.
   Gave up: nothing significant. Other option (fixed window counter, `INCR` + `EXPIRE`) is simpler but allows
   2× bursts at window edges.

8. **Card hold for quotas.** Chose: reserve-then-settle. Gave up: some complexity (hold column, rebuild
   logic, settle path). Other option ("check before, add after") overshot by up to N concurrent calls in
   testing.

9. **One `started_at` per call.** Chose: a single timestamp decides the month everywhere. Gave up: using
   Postgres `now()` as a default. Prevents calls across midnight on the 1st being split across months.

10. **Redis down → 503 for everyone.** Chose: spend safety. Gave up: availability during a Redis outage. With
    1-second timeouts, the outage is at least fast and visible.

11. **Separate Terraform roots (bootstrap vs cluster).** Chose: the protective resources (state bucket, budget
    alerts) can't be destroyed by the routine per-session destroy. Gave up: a single `apply`. Other option
    better when: nothing is destroyed routinely.

12. **Private workers behind NAT, public API endpoint.** Chose: no way into the nodes from the internet,
    while kubectl still works from a laptop. Gave up: the extra isolation of a private endpoint + bastion.

13. **Argo CD pull-based GitOps with prune + selfHeal.** Chose: Git as the single source of truth, CI without
    cluster credentials, drift reverted. Gave up: quick manual fixes (they get reverted) and some safety
    (prune deletes what's removed from Git, including the DB).

---

## 21. Glossary

| Term | Meaning |
|---|---|
| **Tenant** | One caller of the gateway (a team, app or customer) with its own key, limits and usage. |
| **Upstream** | The service the gateway forwards to (OpenRouter now). |
| **Token (LLM)** | The unit models read and write, roughly ¾ of an English word; what providers bill by. |
| **Token bucket** | Rate-limit algorithm: a jar of tickets that refills at a steady rate; each request takes one. |
| **Burst** | Jar size: how many requests can arrive at once. |
| **Card hold** | Reserving estimated tokens before a call, then settling to the real count after. |
| **Lua script (Redis)** | A small program Redis runs as one uninterruptible step, used for atomic read-decide-write. |
| **Atomic** | Happens as one indivisible step; nothing else can run in the middle. |
| **SSE (Server-Sent Events)** | A long HTTP response where the server writes events as they happen; how LLM streaming works. |
| **Fail closed / fail open** | Refuse / allow a request when a dependency is broken. |
| **Reconciliation** | Comparing our computed cost with the provider's reported cost to catch drift. |
| **Monotonic clock** | A clock that only moves forward (`perf_counter`); safe for measuring durations. |
| **Liveness / readiness probe** | Kubernetes health checks: "restart me if this fails" / "send me traffic only if this passes". |
| **GitOps** | Git holds the desired state; an in-cluster agent (Argo CD) makes the cluster match it. |
| **Prune / selfHeal (Argo CD)** | Delete objects removed from Git / revert manual edits to match Git. |
| **Remote state (Terraform)** | Terraform's record of what it built, kept in a shared bucket instead of a laptop. |
| **VCN / subnet** | OCI's private network (like an AWS VPC) and its address-range "floors". |
| **NAT gateway** | Lets private machines reach the internet outbound, while nothing can connect in. |
| **OKE** | Oracle's managed Kubernetes (like AWS EKS). |
| **Flannel overlay** | Pod networking where pods get addresses from an internal range, not the VCN. |
