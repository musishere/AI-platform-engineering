# Lesson 02: The observability map — Prometheus, OpenTelemetry, Tempo, Grafana, SLOs

> Project 4 (Monitoring). Modules: M17 Observability, M21 LLMOps.
> Lesson 01 was about SLOs. This one zooms out: what each tool is, how they fit together, and how this repo runs them.

**One sentence:** Prometheus counts *how many and how fast* across all requests, OpenTelemetry records *the step-by-step story of one request*, Tempo stores those stories, Grafana shows both on one screen, and an SLO is the target line drawn on top of Prometheus's numbers.

---

## 1. Three different questions

🟢 **In simple words**

When something goes wrong in production, you ask three different questions, in this order:

1. **"Is something wrong?"** You need numbers over time: error %, speed, traffic.
2. **"Where exactly, for which requests?"** You need the step-by-step story of a bad request.
3. **"What exactly happened?"** You need the details: the message, the error text.

No single tool answers all three well. Each one is built for one question.

**Analogy: a hospital.**

| Hospital | Your gateway | Question it answers |
|---|---|---|
| Target: "95% of patients seen within 30 minutes" | **SLO** | Are we good enough? |
| Vital-signs monitor: heart rate and blood pressure as numbers over time | **Metrics** (Prometheus) | Is something wrong, right now and in trend? |
| The patient's journey chart: reception 10:02 → triage 10:15 → doctor 10:50 | **Trace** (OpenTelemetry → Tempo) | Where did *this* patient wait? |
| The doctor's written notes | **Logs** | What exactly was said and done? |
| The big screen at the nurses' station showing all of it | **Grafana** | One place to look |
| The billing department's invoice for each patient | **`usage_events` table** (Postgres) | What do we charge? (not observability, see section 6) |

🔵 **Technically**

Metrics, traces and logs are called the **three pillars of observability** (observability = being able to work out what's happening inside a system from what it outputs, without adding new code first).

| Pillar | Data shape | Cheap at scale? | Good for |
|---|---|---|---|
| **Metrics** | Numbers, pre-aggregated: "312 requests with status 504 so far" | ✅ Very. The cost doesn't grow with traffic, only with how many label combinations there are | Dashboards, alerts, SLOs, trends |
| **Traces** | One record per request, made of timed steps (**spans**) | ⚠️ Grows with traffic. Big systems keep only a sample | "Why was *this* request slow?" |
| **Logs** | Lines of text/JSON per event | ⚠️ Grows with traffic | Exact error messages, details |

---

## 2. The whole picture

```
                        YOUR GATEWAY POD (namespace llm-gateway)
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  FastAPI app (port 8000)                                                    │
 │    │                                                                        │
 │    ├── MetricsMiddleware ──▶ counters in memory ──▶ /metrics (port 9100)    │
 │    │     (metrics.py)                                     ▲                 │
 │    │                                                      │ ① PULL every 15s│
 │    ├── OpenTelemetry SDK ──▶ spans batched in memory ──┐  │                 │
 │    │     (tracing.py)                                  │  │                 │
 │    │                                                   │ ② PUSH (OTLP/HTTP) │
 │    └── logging ──▶ stdout ─────────────────────────┐   │  │                 │
 └────────────────────────────────────────────────────│───│──│─────────────────┘
                                                      │   │  │
              ③ kubectl logs (no log database)  ◀─────┘   │  │
                                                          │  │
                   namespace monitoring                   ▼  │
 ┌──────────────────────────────────────────────────────────────────────────┐
 │   Tempo  (:4318 in, :3200 query)              Prometheus                  │
 │   stores traces, 24h, no disk                 stores metrics, 3d, no disk │
 │        ▲                                      ▲   ▲                       │
 │        │                       PodMonitor ────┘   │  (tells it what to    │
 │        │                       (k8s/gateway-      │   scrape)             │
 │        │                        podmonitor.yaml)  │                       │
 │        │                                          │ SLO = a query on this │
 │        │ ④ read                         ④ read    │ data (lesson 01)      │
 │        └────────────────┬─────────────────────────┘                       │
 │                     Grafana  ◀── dashboard JSON from a ConfigMap          │
 │                        │         (k8s/gateway-dashboard.yaml)             │
 │                     Alertmanager (installed, unused: alerts are parked)   │
 └────────────────────────│─────────────────────────────────────────────────┘
                          ▼
              you, at localhost:3000 (port-forward)
```

The four arrows are the whole story:

1. **Prometheus pulls metrics** from each pod.
2. **The gateway pushes traces** to Tempo.
3. **Logs go to stdout**, and you read them with `kubectl logs`.
4. **Grafana reads from both** stores and draws them.

---

## 3. Each piece, one at a time

### 3.1 Prometheus — the counter

🟢 **In simple words:** a database of numbers over time. Every 15 seconds it visits each gateway pod, reads the current counters ("requests so far: 1,204"), and saves them with a timestamp. Graphs and SLOs come from comparing those snapshots.

🔵 **Technically**
- **Pull model:** Prometheus scrapes `http://<pod>:9100/metrics`. The app only keeps numbers in memory and serves them. If Prometheus is down, the app doesn't notice or care.
- **Metric types in [metrics.py](../../gateway/metrics.py):** `Counter` (only goes up: `gateway_requests_total`), `Histogram` (counts per "≤ X seconds" bucket: duration, TTFT), `Gauge` (goes up and down: `gateway_requests_in_flight`).
- **Labels** split a metric: `gateway_requests_total{tenant="4", model="…", status="504"}`. Each distinct label combination is stored as a separate **time series** (one line on a graph).
- **PromQL** is the query language: `rate(...)`, `increase(...)`, `histogram_quantile(...)`.
- **Prometheus Operator:** you never edited a Prometheus config file. The operator (installed with the monitoring stack) watches **PodMonitor** objects and writes the scrape config for you.

### 3.2 OpenTelemetry — the storyteller (a standard, not a database)

🟢 **In simple words:** OpenTelemetry (OTel) is a **standard plus a library** for recording the story of each request: "auth took 4 ms, the Redis check took 1 ms, OpenRouter took 3.2 s". It doesn't store anything itself. It packs the story up and sends it somewhere.

Think of it like a **universal plug**. Because OTel is a standard, the same code can send traces to Tempo today, or to Jaeger or Datadog tomorrow, by changing one setting, not your code.

🔵 **Technically**
- **Trace** = one request. **Span** = one timed step, with a name, start/end time, attributes (`tenant.id = 4`) and a parent span. Spans nest into a tree.
- **Instrumentation** in [tracing.py](../../gateway/tracing.py):
  - *Automatic*: libraries that patch FastAPI, httpx, asyncpg and redis, so every request, upstream call, SQL query and Redis command becomes a span for free.
  - *Manual*: `@tracer.start_as_current_span("auth.find_tenant")` on your own steps, so the timeline reads in your words.
- **OTLP** = OpenTelemetry's wire format. You send it over HTTP to `http://tempo.monitoring:4318` (set in [config.yaml](../../k8s/config.yaml)).
- **Push model** with `BatchSpanProcessor`: spans go into a memory queue and a background thread sends them every few seconds, never inside the request.
- **Context propagation:** the httpx instrumentation adds a `traceparent` header to upstream calls. If OpenRouter also used OTel, its spans could join your trace. This is how one trace crosses several services (M17 interview Q3).

### 3.3 Tempo — the trace warehouse

🟢 **In simple words:** a database built only for traces. It stores them cheaply and finds them by trace ID or by attributes ("all traces where tenant.id = 4").

🔵 **Technically:** Grafana Tempo in single-binary mode ([argocd/tempo.yaml](../../argocd/tempo.yaml)): one pod receives on :4318 and answers queries on :3200. Kept for 24h, no disk.

### 3.4 Grafana — the screen

🟢 **In simple words:** Grafana stores no data of its own. It's a window that asks Prometheus and Tempo questions and draws the answers.

🔵 **Technically:** two **data sources** (Prometheus, set up by the chart; Tempo, added in [monitoring.yaml](../../argocd/monitoring.yaml)). Your dashboard is JSON inside a ConfigMap with the label `grafana_dashboard: "1"`. A **sidecar** container (a helper that runs next to Grafana in the same pod) finds labelled ConfigMaps and loads them, so the dashboard lives in Git.

### 3.5 SLO — a rule, not a tool

🟢 **In simple words:** nothing new gets installed for an SLO. It's a **PromQL query** (good ÷ counted) plus a **target** (99%) plus a **policy** (what you do when the budget runs out). Prometheus does the math; Grafana shows it.

🔵 **Technically:** see lesson 01, section 7. Production setups often save the SLI as a **recording rule** (Prometheus computes and stores the query result every minute, so dashboards are fast) and alert on **burn rate** through Alertmanager. Both are parked for now.

### 3.6 Logs — the part we kept minimal

`metering.py` and `limits.py` write Python `logging` lines to stdout, for things like "usage write failed". You read them with `kubectl logs`. There's **no log database** (like Loki). That's a deliberate skip: traces already cover "what happened in this request", and a log store is one more thing to run. When you need to search logs across all pods and days, add Loki.

---

## 4. Pull vs push, and what happens when something is down

| | Metrics (Prometheus) | Traces (OTel → Tempo) |
|---|---|---|
| Direction | Prometheus **pulls** from the app | App **pushes** to Tempo |
| Monitoring tool is down | Scrapes are missed, so there's a gap in the graph. Counters keep counting in the app, so the next scrape catches up on totals | Spans are **dropped**. Requests carry on (fail open) |
| App pod dies | Its in-memory counters are lost; `increase()` handles the reset | Spans still in the batch queue are lost (`shutdown()` in `main.py` flushes them on a *clean* stop) |
| Why this direction | Prometheus decides the pace, and it detects a dead pod (`up == 0`) | Spans are made at request time and can't wait for someone to ask |

### Worked example A: Tempo crashes for 10 minutes

- The gateway: every request still succeeds. The batch processor fails to send and drops those spans. **No tenant is affected.** That's the "fail open" choice in `tracing.py`: losing a trace is fine, failing a call is not.
- Prometheus and the SLO: unaffected.
- Grafana: no traces for those 10 minutes.

### Worked example B: the Prometheus pod restarts

- It has **no disk**, so the stored history (up to 3 days) is gone. Dashboards start empty, and a 1-day SLI is wrong until a day has passed.
- The gateway's counters are untouched; Prometheus picks them up at the next scrape.
- **Billing is safe:** every call's tokens and cost are in Postgres `usage_events`, which has its own disk. Metrics are for watching, not for invoicing.

---

## 5. Why the tenant ID can be a metric label, but the request ID can't

🟢 **In simple words:** every different label value creates a new line that Prometheus stores and keeps updating. 5 tenants = 5 lines, fine. A request ID is different every time = a new line *per request* = millions of lines, and Prometheus runs out of memory. A trace is stored once and never updated, so any unique value is fine there.

🔵 **Technically:** this is **cardinality** (the number of distinct label combinations). Metric cost ≈ the product of every label's values: `tenants × models × statuses` (× 13 buckets for histograms). Your rule in `metrics.py`: labels only come from small known sets, and unknown models become `"other"`. Free-form values (request ID, prompt text, user ID at scale) go on **spans as attributes**, like `tenant.id` and `llm.model` in `main.py`.

### Worked example C: where does each value go?

| Value | Metric label? | Span attribute? | Why |
|---|---|---|---|
| tenant id (a handful) | ✅ | ✅ | Small known set |
| model, only known names | ✅ (`model_label`) | ✅ | Unknown names become "other" |
| HTTP status | ✅ | ✅ (auto) | ~10 values |
| pod name | ✅ (automatic in Prometheus) | ✅ (resource) | Few, and it shows canary vs stable |
| request / trace ID | ❌ | ✅ | Unique per request, so cardinality explodes |
| the caller's model string as sent | ❌ | ✅ | The caller controls it, so it could be anything |

---

## 6. Why `usage_events` isn't "just another metric"

You record tokens **twice**: in Prometheus (`gateway_tokens_total`) and in Postgres (`usage_events`). That isn't duplication. They have different jobs:

| | Prometheus `gateway_tokens_total` | Postgres `usage_events` |
|---|---|---|
| Job | Watch trends: "tokens/min is spiking" | Bill, and enforce quotas: "tenant 4 used 812,331 tokens in October" |
| Exact? | Approximate: lost on pod restart, sampled every 15s | Exact: one row per call, written in the request path |
| Kept | 3 days, no disk | Forever, on a disk |
| If it fails | A graph gap | You lose money or over-charge, so the request **fails closed** (503) |

Interview line: "Metrics are allowed to be lossy, billing is not. So billing lives in the database inside the request, and metrics live outside it."

---

## 7. How they work together: one debugging story

This is the order you'll actually use them in (M17 interview Q1: "users say it got worse").

### Worked example D: tenant 4 complains about failures

```
 ① SLO panel        "Availability today: 97.1%. Budget: −190% (exhausted)"
       │             → something IS wrong, and it matters (lesson 01)
       ▼
 ② Metrics          Error-rate-by-tenant panel: only tenant 4. Status: 504.
       │             TTFT panel normal for others → not the whole system
       ▼
 ③ Traces           Grafana → Explore → Tempo:  tenant.id = 4, status 504
       │             one trace:  POST /v1/messages ............. 120.0s
       │                          ├ auth.find_tenant (SELECT) ..   0.004s
       │                          ├ limits.rate_limit (EVALSHA) .  0.001s
       │                          ├ metering.start (INSERT) .....  0.006s
       │                          └ POST openrouter.ai ......... 120.0s ✗ timeout
       ▼
 ④ Cause            Our own steps are fast; the upstream call hit the 120s timeout.
                    Tenant 4's spans show huge max_tokens + a big model → long answers.
       ▼
 ⑤ Action           Short term: tell the tenant / raise the timeout for that model.
                    Long term: a fallback model (Project 9). Postmortem: budget spent by upstream.
```

Each tool did one job: the **SLO** said "act now", **metrics** said "who and what", the **trace** said "which step", and only then did you change anything.

---

## 8. How this repo manages it all

| Piece | Installed by | File | Choices we made | Known ceiling (`ponytail:` notes) |
|---|---|---|---|---|
| Prometheus + Grafana + Alertmanager | Argo CD renders the `kube-prometheus-stack` Helm chart, pinned at 91.8.2 | [argocd/monitoring.yaml](../../argocd/monitoring.yaml) | Reads every PodMonitor in the cluster; Grafana admin from a hand-made Secret; control-plane scrapes off on minikube | 3-day retention, **no disk**. Applied by hand once (no "app of apps") |
| Tempo | Argo CD, `tempo` chart 3.1.0 | [argocd/tempo.yaml](../../argocd/tempo.yaml) | Single-binary mode, 24h retention, no usage stats sent | **No disk**, so traces vanish on restart |
| What to scrape | Argo CD, with the gateway | [k8s/gateway-podmonitor.yaml](../../k8s/gateway-podmonitor.yaml) | PodMonitor on port 9100, so metrics never leave through the public Service | — |
| Metrics code | In the app | [gateway/metrics.py](../../gateway/metrics.py) | `prometheus_client`; raw ASGI middleware to time the real last byte of a stream; LLM-sized buckets up to 120s; unknown models → "other" | Tenant label on the histogram multiplies series |
| Tracing code | In the app | [gateway/tracing.py](../../gateway/tracing.py) | OTel SDK; auto + named spans; skip `/health` and per-chunk spans; fail open | **100% of requests traced**; add sampling at scale |
| Where traces go | ConfigMap | [k8s/config.yaml](../../k8s/config.yaml) | `OTEL_EXPORTER_OTLP_ENDPOINT` unset = tracing off, so local runs need no Tempo | — |
| Dashboard | Argo CD, as a ConfigMap | [k8s/gateway-dashboard.yaml](../../k8s/gateway-dashboard.yaml) | Dashboard in Git, loaded by Grafana's sidecar | UI-only edits are lost on restart |
| Logs | stdout | `metering.py`, `limits.py` | `kubectl logs` only | No log database |
| SLO | — (next step) | dashboard query | 1-day window, see lesson 01 | Burn-rate alerts parked |

**Rule of thumb in this repo:** the *monitoring tools* are in `argocd/` (installed once, shared), while the *gateway's own monitoring* (PodMonitor, dashboard) is in `k8s/`, next to the gateway. When the app changes, its monitoring changes in the same commit.

---

## 9. Trade-offs (your interview stories)

| Decision | What we gave up | When the other option wins |
|---|---|---|
| **Metrics via `prometheus_client`, traces via OTel**, instead of OTel for both | One standard for everything. OTel can do metrics too, but the Prometheus client is simpler and the stack already speaks it | Many services in several languages sending to a vendor, where one OTel Collector pipeline is simpler |
| **Self-hosted** (Prometheus/Tempo/Grafana), not SaaS (Datadog, Grafana Cloud) | Zero-ops, long retention, built-in SLO features | Real company: SaaS often costs less than an engineer's time running the stack |
| **No disks** on Prometheus/Tempo | History survives restarts | Anything real: add a PVC (a disk claim) |
| **Trace 100%** | Lower trace storage cost | High traffic: keep ~1–10% (sampling), and always keep errors and slow calls (**tail sampling**) |
| **No log database** | Searching logs across pods and days | When incidents need log search, or for audit requirements |

---

## 10. What an *applied AI* engineer adds on top (M17, preview)

What you have now is **system observability**: is the gateway up, fast and fair? For an AI *app* (Projects 5–9), M17 asks for more on each trace:
- the **prompt version** and model, **cached tokens**, **cost**
- the **input and output text**, with PII redacted (personal data removed) before it's stored
- tool calls and retrieval steps as their own spans
- **quality signals**: user feedback, LLM-judge scores on sampled traffic

Tools like Langfuse and Arize Phoenix are OTel-based tracing built for exactly that, and the **OpenTelemetry GenAI semantic conventions** are standard attribute names like `gen_ai.request.model` and `gen_ai.usage.input_tokens`. Because you already use OTel, adding these later is extra attributes and maybe a second exporter, not a rewrite. We'll do it when Project 5 needs it.

---

## 11. Self-check quiz

1. For each question, which tool answers it: (a) "is the error rate higher than yesterday?" (b) "why did *this* call take 40 seconds?" (c) "what exact error did the limiter log?" (d) "how many tokens should tenant 4 be billed for this month?"
2. Why is `tenant` OK as a metric label but a request ID isn't? Where does the request ID go instead?
3. Tempo is down for 10 minutes. What happens to tenants' requests, and to the SLO?
4. The Prometheus pod restarts. What's lost? Is billing affected? Why?
5. Tenant 4 reports failures. In what order do you use SLO, metrics and traces, and what does each tell you?
6. "We use Prometheus for metrics, so why OpenTelemetry too?" Answer in two sentences.

<details><summary>Answers</summary>

1. (a) Metrics/Prometheus. (b) A trace in Tempo. (c) Logs (`kubectl logs`). (d) Postgres `usage_events`, not Prometheus, which is lossy. (Section 1, section 6.)
2. Each label value is a separate stored time series. Tenants are a handful; request IDs are unique per request, so the series count explodes and Prometheus runs out of memory. Put it on the trace as a span attribute. (Section 5, example C.)
3. Requests succeed: spans are dropped and the batch processor fails open. The SLO is unaffected, because it comes from Prometheus. Only the traces for those 10 minutes are missing. (Example A.)
4. Up to 3 days of metric history (no disk), so the 1-day SLI is unreliable until a day passes. Billing is fine: it's in Postgres, written in the request path. (Example B, section 6.)
5. SLO → "is it bad enough to act?" Metrics → "which tenant, which status, is it everyone or one?" Traces → "which step in the request failed?" Then act. (Example D.)
6. They answer different questions: Prometheus gives cheap totals and trends across *all* requests, while OTel records the step-by-step story of *one* request. Together they cover "is something wrong" and "where exactly". (Sections 1, 3.)

</details>

---

## Key terms

| Simple | Technical |
|---|---|
| Numbers over time | Metrics, time series |
| Prometheus visits the app to read numbers | Pull / scrape |
| The app sends its data out | Push (OTLP) |
| The story of one request | Trace |
| One timed step in that story | Span |
| A note on a step (tenant 4) | Span attribute |
| Code that creates spans for you | Auto-instrumentation |
| Passing the trace ID to the next service | Context propagation (`traceparent`) |
| A split of a metric (per tenant) | Label |
| How many label combinations exist | Cardinality |
| "Scrape these pods" as a k8s object | PodMonitor (Prometheus Operator) |
| Keeping only some traces | Sampling (head / tail) |
| Keep working when monitoring is down | Fail open |
