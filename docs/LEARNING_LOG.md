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

## 2026-09-28: Project 3 start, Oracle Cloud (Terraform)
- **Built:** switched from AWS to Oracle Cloud (Mumbai, Always Free + $300/30-day trial). `infra/bootstrap` (permanent, free): compartment, versioned private state bucket with `prevent_destroy`, $20/$50 budget alerts; its state moved into the bucket. `infra/cluster` (per session): VCN with 3 subnets (public API, public LB, private workers), internet/NAT/service gateways, Oracle's reference security rules; OKE basic cluster v1.36.1 + 2×ARM node pool.
- **Learned:** Terraform plan → apply → destroy and state; remote state between folders; new-compartment permissions take minutes to spread (flickering 404s, so wait for N successes in a row); "not found" can mean "not allowed"; NAT = out only; the Kubernetes API endpoint is the cluster's control desk, not a gateway.
- **Broke:** worker nodes never started: Mumbai OUT_OF_HOST_CAPACITY for free ARM (and even the free AMD micro), confirmed with a capacity report. Also misread the session expiry (local time, not UTC), so a destroy and watcher failed on an expired login.
- **Next:** wait for free ARM capacity (watcher running), then rebuild the cluster and check `kubectl get nodes`. Everything in `infra/cluster` destroyed; only bootstrap left (free).

## 2026-09-29: Project 3, CI (build, scan, push) + technical.md
- **Built:** `technical.md` (whole-project deep reference). GitHub Actions pipeline: build → Trivy scan (fail on fixable HIGH/CRITICAL, before push) → push `ghcr.io/…/gateway:<sha>` → CI commits the tag into `k8s/gateway.yaml` → Argo CD rolls it out. Private GHCR image pulled via a read-only `ghcr-pull` Secret. First run: push → new pods in ~5 min.
- **Learned:** CI builds artifacts, Argo CD only deploys manifests (code is invisible to it; the tag commit is the link); unique SHA tags make `IfNotPresent` safe; GITHUB_TOKEN commits don't trigger workflows (no loop); `paths` filter means k8s-only changes skip CI; Argo CD polls every ~3 min; secrets never go through chat (leaked PAT → revoked).
- **Broke:** new gateway pod crashed once at startup: Postgres's exec readiness probe used the 1s default timeout, flapped under node load (68× in 5h), dropped out of the Service → connection refused. Fixed with `timeoutSeconds: 5` on Postgres and Redis probes.
- **Next:** verify quota rebuild after the Redis restart with a real call, then canary release (Argo Rollouts).

## 2026-09-29/30: Project 3, canary releases (Argo Rollouts)
- **Built:** the gateway is a Rollout, not a Deployment: 4 pods so each canary step is 25%, a `gateway-canary` Service that points only at new pods, a smoke test (own file, so CI's `sed` doesn't rewrite its image), automatic rollback if it fails. Startup probe plus 3s probe timeouts; `maxUnavailable: 0`, `maxSurge: 1`.
- **Learned:** without a traffic router the split IS the pod count; a rolling update only checks `/health`, so a buggy version reaches 100%; the startup probe holds off liveness until boot finishes; CRDs over 256 KB need server-side apply.
- **Broke:** liveness judged pods from second 0 with a 1s timeout and killed all 4 booting gateways; the default `maxUnavailable: 25%` dropped capacity to 3 pods during canary steps.
- **Next:** Project 4, Prometheus metrics.

## 2026-09-30/10-01: Project 4, metrics + Grafana dashboard
- **Built:** `gateway/metrics.py` (requests, duration, TTFT, tokens, 429s, in-flight) on its own port 9100; kube-prometheus-stack via Argo CD; a PodMonitor; a per-tenant Grafana dashboard (rate, 5xx %, p50/95/99, TTFT, tokens/min, 429s).
- **Learned:** Prometheus pulls; label cardinality (only known values become labels, unknown models become "other"); a raw ASGI middleware sees the real last byte of a stream; LLM-sized histogram buckets (up to 120s).
- **Broke:** Grafana restarted every 1–2 min (the chart made a new random password on every render) → `existingSecret`; Grafana OOMKilled at 256 Mi when the UI opened → 512 Mi.
- **Next:** OpenTelemetry traces.

## 2026-10-02: Project 4, OpenTelemetry tracing (code written, not deployed yet)
- **Built:** `gateway/tracing.py` (OTel SDK, auto spans for FastAPI/httpx/asyncpg/redis, named spans for auth, limits and metering, tenant.id and pod name on traces); Tempo single-binary via `argocd/tempo.yaml`; a Tempo data source in Grafana.
- **Learned:** spans are pushed in batches (unlike Prometheus, which pulls); tracing fails open (dropped spans never fail a call); free-form values are fine on traces but not as metric labels; skip `/health` and per-chunk spans or the noise buries real traces.
- **Broke:** nothing yet. The Tempo chart had moved from `grafana` to `grafana-community` (deprecated in January 2026).
- **Next:** start minikube, apply `argocd/tempo.yaml` + `argocd/monitoring.yaml`, push, and follow one request end to end in Grafana Explore.
