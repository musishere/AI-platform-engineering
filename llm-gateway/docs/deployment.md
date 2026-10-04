# Deployment: how the whole platform runs

> **Status (2026-10-01):** everything below runs on **minikube on my Mac, for $0**. Projects 1–2 (gateway,
> quotas) are built. Project 3 (deploy) is done except the OKE cluster itself (blocked on Oracle Cloud
> capacity): CI builds, scans and pushes every code change, Argo CD deploys it, and Argo Rollouts releases
> it as a canary. Project 4 (monitoring) has started: the gateway exports metrics, Prometheus scrapes them,
> and a Grafana dashboard shows them per tenant.
>
> All numbers in this document (pod names, versions, IPs, counts) are real values from my cluster on this
> date, not examples.

**One-sentence version:** my gateway code becomes an **image** (built by CI), Kubernetes runs **4 copies**
of it next to **Postgres** and **Redis**, **Argo CD** keeps the cluster identical to Git, **Argo Rollouts**
releases new versions gradually, and **Prometheus + Grafana** watch all of it.

**How to read this:** every concept goes 🟢 **In simple words** first, then 🔵 **Technically**. Part 1 is
the map. Parts 2–4 are one layer each. Part 5 is for operating it: rebuild, commands, debugging, decisions.

---

## Table of contents

**Part 1: The big picture**
1. [Where everything physically runs](#1-where-everything-physically-runs)
2. [Inventory: every namespace and every pod](#2-inventory-every-namespace-and-every-pod)
3. [The one idea behind everything: desired state + control loops](#3-the-one-idea-behind-everything-desired-state--control-loops)
4. [The whole flow on one page](#4-the-whole-flow-on-one-page)
5. [Life of one request](#5-life-of-one-request)

**Part 2: Kubernetes (namespace `llm-gateway`)**
6. [The files in `k8s/`](#6-the-files-in-k8s)
7. [The gateway: Rollout, pods, Services](#7-the-gateway-rollout-pods-services)
8. [Health checks: startup, liveness, readiness](#8-health-checks-startup-liveness-readiness)
9. [Resources: requests and limits](#9-resources-requests-and-limits)
10. [Postgres](#10-postgres)
11. [Redis](#11-redis)
12. [Config and Secrets](#12-config-and-secrets)
13. [Networking: how `postgres` becomes a working connection](#13-networking-how-postgres-becomes-a-working-connection)
14. [Storage: what "the disk survives" means](#14-storage-what-the-disk-survives-means)
15. [Kubernetes under the hood: what happens on `apply`](#15-kubernetes-under-the-hood-what-happens-on-apply)

**Part 3: Delivery (CI → Argo CD → Argo Rollouts)**
16. [The image](#16-the-image)
17. [CI: GitHub Actions](#17-ci-github-actions)
18. [Argo CD: setup](#18-argo-cd-setup)
19. [Argo CD: how it works](#19-argo-cd-how-it-works)
20. [Argo Rollouts: canary releases](#20-argo-rollouts-canary-releases)
21. [One code push, end to end](#21-one-code-push-end-to-end)

**Part 4: Monitoring (Prometheus + Grafana)**
22. [Why monitoring, and the three kinds of signal](#22-why-monitoring-and-the-three-kinds-of-signal)
23. [What the gateway measures](#23-what-the-gateway-measures)
24. [How the gateway produces its metrics (the code)](#24-how-the-gateway-produces-its-metrics-the-code)
25. [The monitoring stack: what's installed](#25-the-monitoring-stack-whats-installed)
26. [How Prometheus finds and scrapes the gateway](#26-how-prometheus-finds-and-scrapes-the-gateway)
27. [How Prometheus stores data](#27-how-prometheus-stores-data)
28. [Grafana and the gateway dashboard](#28-grafana-and-the-gateway-dashboard)
29. [Alerting (what exists so far)](#29-alerting-what-exists-so-far)

**Part 5: Operating it**
30. [Rebuild everything from zero](#30-rebuild-everything-from-zero)
31. [Command cheat sheet](#31-command-cheat-sheet)
32. [Troubleshooting](#32-troubleshooting)
33. [What broke while building this, and the fixes](#33-what-broke-while-building-this-and-the-fixes)
34. [Decisions and tradeoffs (interview stories)](#34-decisions-and-tradeoffs-interview-stories)
35. [Known limits](#35-known-limits)
36. [Later: minikube → OKE](#36-later-minikube--oke)
37. [Check yourself](#37-check-yourself)
38. [Key terms](#38-key-terms)

---
---

# Part 1: The big picture

## 1. Where everything physically runs

🟢 **In simple words:** Russian dolls. My Mac holds a small Linux computer (Colima), which holds a pretend
Kubernetes computer (minikube), which holds all my apps. A doll can never be bigger than the one it sits in.

```
┌─ MacBook (Intel, 4 cores, 16 GB, macOS) ───────────────────────────────────────────────┐
│  terminal: kubectl, docker, minikube, uv, git                                          │
│  browser:  Grafana (localhost:3000), Argo CD (localhost:8080) via port-forward         │
│                                                                                        │
│  ┌─ Colima VM (Linux, 4 CPU, 6 GB), colima 0.10.1 ─────────────────────────────────┐   │
│  │   runs the Docker engine                                                       │   │
│  │                                                                                │   │
│  │   ┌─ container "minikube" = ONE Kubernetes node (k8s v1.35.1, minikube 1.38.1) ┐ │   │
│  │   │   control plane: API server, etcd, scheduler, controller-manager           │ │   │
│  │   │   kubelet + container runtime                                               │ │   │
│  │   │                                                                             │ │   │
│  │   │   ns kube-system   (6 pods)   Kubernetes itself                             │ │   │
│  │   │   ns argocd        (7 pods)   GitOps deployer                               │ │   │
│  │   │   ns argo-rollouts (1 pod)    canary controller                             │ │   │
│  │   │   ns llm-gateway   (6 pods)   gateway ×4, postgres, redis   ← MY APP        │ │   │
│  │   │   ns monitoring    (6 pods)   Prometheus, Grafana, Alertmanager, …          │ │   │
│  │   └─────────────────────────────────────────────────────────────────────────────┘ │   │
│  └────────────────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────────────────┘
          ▲                                   │
          │ git push                          │ gateway calls Claude
     GitHub (repo, Actions, GHCR)        OpenRouter → Claude
```

🔵 **Technically:** macOS can't run Linux containers natively, so **Colima** runs a Linux VM with the
Docker engine. Minikube's `docker` driver runs the whole Kubernetes node as **one Docker container** inside
that VM. The control plane and all my pods share that single node.

**Why 6 GB for Colima:** Kubernetes (~0.7 GB) + Argo CD (~0.5–1 GB) + the monitoring stack (~1–1.5 GB) +
Postgres + Redis + 4 gateways. Squeezed smaller, pods sit in `Pending` (no room to schedule) or get
`OOMKilled`, and it looks like a bug in my YAML.

**Things that live outside the cluster:**

| Where | What |
|---|---|
| GitHub repo `musishere/AI-platform-engineering` | All code and manifests: the **single source of truth** |
| GitHub Actions | CI: builds, scans and pushes the image |
| GHCR (`ghcr.io/musishere/ai-platform-engineering/gateway`) | Private image registry |
| OpenRouter | The upstream LLM API (routes to Claude) |
| Oracle Cloud (`infra/`) | Terraform state bucket + budget alerts (permanent). The OKE cluster is written but not running |

---

## 2. Inventory: every namespace and every pod

🟢 **In simple words:** a **namespace** is a folder inside the cluster. It groups related things, and
deleting the folder deletes everything in it.

🔵 **All 26 pods, by namespace** (`kubectl get pods -A`):

| Namespace | Pod | Count | What it is | Installed by |
|---|---|---|---|---|
| **llm-gateway** | `gateway-7cc469c5ff-…` | **4** | My FastAPI gateway | Argo CD (from `k8s/`) |
| | `postgres-…` | 1 | Tenants, usage events, quota holds | Argo CD |
| | `redis-…` | 1 | Rate-limit buckets, quota counters | Argo CD |
| **argocd** | `argocd-application-controller-0` | 1 | Compares Git with the cluster, applies differences | Manual bootstrap |
| | `argocd-repo-server` | 1 | Clones Git / renders Helm charts into objects | Manual bootstrap |
| | `argocd-server` | 1 | Web UI + API | Manual bootstrap |
| | `argocd-redis` | 1 | Argo CD's own cache (**not** the gateway's Redis) | Manual bootstrap |
| | `argocd-dex-server` | 1 | SSO login (unused) | Manual bootstrap |
| | `argocd-applicationset-controller` | 1 | Generates many Applications from a template (unused) | Manual bootstrap |
| | `argocd-notifications-controller` | 1 | Slack/email on events (unused) | Manual bootstrap |
| **argo-rollouts** | `argo-rollouts-…` | 1 | Runs canary releases | Manual bootstrap |
| **monitoring** | `prometheus-monitoring-kube-prometheus-prometheus-0` | 1 | Scrapes and stores metrics | Argo CD (Helm chart) |
| | `monitoring-grafana-…` | 1 | Dashboards (3 containers: Grafana + 2 loaders) | Argo CD (Helm chart) |
| | `alertmanager-monitoring-kube-prometheus-alertmanager-0` | 1 | Routes alerts to people | Argo CD (Helm chart) |
| | `monitoring-kube-prometheus-operator-…` | 1 | Turns PodMonitors etc. into Prometheus config | Argo CD (Helm chart) |
| | `monitoring-kube-state-metrics-…` | 1 | Metrics *about* Kubernetes objects | Argo CD (Helm chart) |
| | `monitoring-prometheus-node-exporter-…` | 1 | Metrics about the node (CPU, disk, memory) | Argo CD (Helm chart) |
| **kube-system** | `kube-apiserver`, `etcd`, `kube-scheduler`, `kube-controller-manager`, `kube-proxy`, `coredns` | 6 | Kubernetes itself (section 15) | minikube |

**Total resources requested on the node:** CPU **1460m (36%)**, memory **1690 Mi (28%)**. Plenty of headroom.

**Services in `llm-gateway`** (stable addresses, section 13):

| Service | Cluster IP | Port → pod port | Points at |
|---|---|---|---|
| `gateway` | 10.97.42.26 | 80 → 8000 | **All** gateway pods (stable + canary) |
| `gateway-canary` | 10.111.219.67 | 80 → 8000 | **Only** canary pods, during a release |
| `postgres` | 10.110.99.166 | 5432 | The Postgres pod |
| `redis` | 10.98.31.225 | 6379 | The Redis pod |

---

## 3. The one idea behind everything: desired state + control loops

🟢 **In simple words:** a thermostat. You don't tell the heater "run for 20 minutes". You say "I want
22°C". The thermostat keeps checking the room and switches the heater on or off until the room matches. If
someone opens a window, it notices and fixes it again. It never "finishes".

🔵 **Technically:** this is a **control loop** (also called **reconciliation**): *observe the actual state
→ compare with the desired state → act on the difference → repeat forever*. Every moving part in this
system is one of these loops, stacked on top of each other:

```
 WHAT I WANT                                                                WHAT ACTUALLY RUNS
 ───────────                                                                ──────────────────
 Git: k8s/*.yaml ─▶ Argo CD loop ─▶ objects in the API server ─▶ controller loops ─▶ kubelet loop ─▶ containers
 "4 gateways,        compares Git    "Rollout gateway,          Rollouts, ReplicaSet,  starts/stops    real
  image :2e42873"    vs cluster      replicas 4"                scheduler, endpoints   containers,     processes
                                                                                       runs probes
```

| Loop | Watches | Desired state comes from | Acts by |
|---|---|---|---|
| Argo CD | Git + cluster | `k8s/` on `main` | Applying / pruning objects |
| Argo Rollouts | Rollout objects | the Rollout's spec | Scaling two ReplicaSets, running analyses |
| ReplicaSet controller | ReplicaSets | `replicas: N` | Creating / deleting pods |
| Scheduler | Unscheduled pods | resource requests | Picking a node |
| kubelet | Pods on its node | the pod spec | Starting containers, running probes, restarting |
| Prometheus operator | PodMonitors, etc. | those objects | Rewriting Prometheus' config |

**Why this matters (interview line):** *imperative* = "do these steps" (breaks if a step fails halfway).
*Declarative* = "this is the end state" (the loops retry until it's true, and repair drift later). I never
"start a pod": I change a number in Git and the loops make it true.

---

## 4. The whole flow on one page

```
                                         ┌───────────────────── GitHub ──────────────────────┐
  me: git push (code) ─────────────────▶ │ repo main                                         │
                                         │   │ gateway/** changed → Actions workflow         │
                                         │   ▼                                               │
                                         │ build image → Trivy scan → push to GHCR :<sha>    │
                                         │   │                                               │
                                         │   └─▶ bot commits image tag into k8s/gateway.yaml │
                                         └───────────────────────┬───────────────────────────┘
                                                                 │ Argo CD polls Git (~3 min)
 ┌────────────────────────────── minikube cluster ───────────────▼────────────────────────────┐
 │  Argo CD: "Git says :<sha>, cluster runs older" → applies the new Rollout spec             │
 │        │                                                                                   │
 │        ▼                                                                                   │
 │  Argo Rollouts: 25% (1 of 4 pods) → smoke test → 2 min → 50% → 2 min → 100%                │
 │        │               fail → abort, back to the stable version                            │
 │        ▼                                                                                   │
 │  kubelet pulls :<sha> from GHCR (ghcr-pull Secret) → pods run                              │
 │        │                                                                                   │
 │        ▼  every 15 s                                                                       │
 │  Prometheus scrapes each gateway pod's :9100/metrics  ──▶  Grafana dashboard               │
 └────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Each layer has one job, and none needs another's credentials:**

| Layer | Job | Never does |
|---|---|---|
| CI (GitHub Actions) | Make the artifact (image) | Touch the cluster |
| Git | Hold the desired state | Run anything |
| Argo CD | Make the cluster match Git | Build images |
| Argo Rollouts | Replace versions safely | Decide *what* version to run |
| Kubernetes | Run what it's told | Know about Git or canaries |
| Prometheus / Grafana | Measure and show | Change anything |

---

## 5. Life of one request

A tenant calls the gateway from my Mac:

```
 my Mac                               cluster (namespace llm-gateway)                       internet
 ──────                               ───────────────────────────────                       ────────
 Anthropic SDK / curl
   │ POST /v1/messages, x-api-key: gw_…
   ▼
 localhost:8000
   │ kubectl port-forward svc/gateway 8000:80   (a debugging tunnel, sticks to ONE pod)
   ▼
 Service gateway :80 ──▶ one gateway pod :8000
                           │ MetricsMiddleware starts the clock, in_flight +1
                           │ 1. hash key, look up tenant ────────────────▶ postgres:5432
                           │ 2. take a rate-limit ticket ─────────────────▶ redis:6379
                           │ 3. hold estimated quota tokens ──────────────▶ redis:6379
                           │ 4. write "before" usage row ─────────────────▶ postgres:5432
                           │ 5. forward ───────────────────────────────────────────────────▶ OpenRouter
                           │ 6. stream the reply back ◀────────────────────────────────────── → Claude
                           │ 7. write tokens + cost, settle the quota ────▶ postgres / redis
                           │ MetricsMiddleware: requests +1, duration observed, in_flight −1
   ◀───────────────────────┘
                           ▲
 Prometheus ───────────────┘ every 15 s reads pod:9100/metrics (a separate port, never via the Service)
```

The code-level details of steps 1–7 are in [technical.md](technical.md). This document is about where and
how it all runs.

---
---

# Part 2: Kubernetes (namespace `llm-gateway`)

## 6. The files in `k8s/`

Argo CD deploys **everything in this folder** into the cluster, and nothing else.

| File | Objects | Purpose |
|---|---|---|
| [00-namespace.yaml](../k8s/00-namespace.yaml) | Namespace `llm-gateway` | The folder for the app. `00-` so plain `kubectl apply -f k8s/` creates it first |
| [config.yaml](../k8s/config.yaml) | ConfigMap `gateway-config` | Non-secret settings: `UPSTREAM_BASE_URL`, `REDIS_URL` |
| [gateway.yaml](../k8s/gateway.yaml) | Rollout `gateway`, Services `gateway` + `gateway-canary` | The app itself (section 7) |
| [gateway-smoke.yaml](../k8s/gateway-smoke.yaml) | AnalysisTemplate `gateway-smoke` | The canary's smoke test (section 20) |
| [gateway-podmonitor.yaml](../k8s/gateway-podmonitor.yaml) | PodMonitor `gateway` | Tells Prometheus to scrape the gateway (section 26) |
| [gateway-dashboard.yaml](../k8s/gateway-dashboard.yaml) | ConfigMap `gateway-dashboard` | The Grafana dashboard as JSON (section 28) |
| [postgres.yaml](../k8s/postgres.yaml) | PVC + Deployment + Service `postgres` | Database (section 10) |
| [redis.yaml](../k8s/redis.yaml) | Deployment + Service `redis` | Counters (section 11) |
| [db-init.yaml](../k8s/db-init.yaml) | ConfigMap `db-init` | **Generated** copy of `db/*.sql`, run by Postgres on first start |

**Not in Git, created by hand once per cluster** (they're secrets): `gateway-secrets`, `ghcr-pull`
(section 12).

**Why the gateway's monitoring and dashboard live in `k8s/`, not in the monitoring setup:** whoever owns
the app owns how it's watched. A change to the gateway and a change to its dashboard go through the same
review and the same deploy.

---

## 7. The gateway: Rollout, pods, Services

### 7.1 Why a Rollout and not a Deployment

🟢 A **Deployment** is a manager with one rule: "always keep N copies of version X". On a new version it
swaps copies one by one, and only checks "does `/health` answer?". A **Rollout** is the same manager with a
release plan: new versions get a small share first, get tested, and grow only if healthy.

🔵 `kind: Rollout` (from Argo Rollouts) has the same pod template as a Deployment, plus a `strategy.canary`
section. The Argo Rollouts controller runs it instead of Kubernetes' Deployment controller (section 20).

### 7.2 Why 4 replicas

- **The canary split is the pod count.** With no traffic router, the `gateway` Service picks pods at
  random, so 1 new pod out of 4 gets about 25% of connections. 2 pods could only do 50%.
- **Safe to run many:** the gateway keeps **no state in memory**; everything lives in Postgres and Redis.
  Any pod can serve any request.
- **Availability:** a pod can crash or be replaced while the others keep serving.

### 7.3 The pod, piece by piece

```yaml
spec:
  securityContext:
    runAsNonRoot: true               # refuse to start if the image runs as root
  imagePullSecrets:
    - name: ghcr-pull                # login for the private GHCR image
  containers:
    - name: gateway
      image: ghcr.io/musishere/ai-platform-engineering/gateway:<git-sha>   # written by CI
      imagePullPolicy: IfNotPresent
      ports:
        - name: http      containerPort: 8000     # the API
        - name: metrics   containerPort: 9100     # Prometheus metrics
      envFrom: gateway-config (ConfigMap)          # UPSTREAM_BASE_URL, REDIS_URL
      env: UPSTREAM_API_KEY, DATABASE_URL (from Secret gateway-secrets)
      securityContext:
        allowPrivilegeEscalation: false
      startupProbe / livenessProbe / readinessProbe  → section 8
      resources: requests 100m CPU / 128Mi, limit 256Mi  → section 9
```

| Setting | Why |
|---|---|
| `image: …:<git-sha>` | One tag per commit. A new commit = a new tag = a changed pod template = a rollout. A reused tag like `:dev` or `:latest` changes nothing, so nothing would roll out |
| `imagePullPolicy: IfNotPresent` | Use the copy already on the node if there is one. **Safe only because SHA tags are never reused**: the same tag always means the same bytes |
| `imagePullSecrets: ghcr-pull` | The image is private. This is the kubelet's login to GHCR |
| `runAsNonRoot` + UID 10001 in the Dockerfile | If someone breaks into the process, they aren't root in the container |
| `allowPrivilegeEscalation: false` | The process can't gain more rights than it started with |
| Two named ports | `http` for callers, `metrics` for Prometheus. Only `http` is behind the Services |
| Only 2 keys from the Secret | The gateway doesn't need `POSTGRES_PASSWORD` on its own. Least privilege |

### 7.4 The two Services

```
Service "gateway"         selector: app=gateway                       → ALL gateway pods (callers use this)
Service "gateway-canary"  selector: app=gateway                       → same, EXCEPT during a release:
                                   + rollouts-pod-template-hash=<new>    Rollouts adds this line, so it
                                                                         matches ONLY the new pods
```

- Both are `ClusterIP`: reachable only inside the cluster. From my Mac I use `kubectl port-forward`.
- Both forward only **port 80 → 8000**. Port 9100 (metrics) is **not** exposed through them, so tenant
  usage data can never leave through a Service (or, later, the public load balancer on OKE).
- `gateway-canary` exists only so the smoke test can reach **only** the new version (section 20).

---

## 8. Health checks: startup, liveness, readiness

🟢 **In simple words:** three questions Kubernetes keeps asking each pod:

| Probe | Question | If it fails |
|---|---|---|
| **Startup** | "Have you finished booting?" | Keep waiting (up to a limit), then restart |
| **Liveness** | "Are you still alive?" | **Kill and restart** the container |
| **Readiness** | "Can you take traffic right now?" | **Remove from the Service** (no traffic), no restart |

🔵 **The gateway's probes** (all call `GET /health` on port 8000):

| Probe | Every | Timeout | Fails after | Effect |
|---|---|---|---|---|
| startup | 2 s | 3 s | 30 tries = **60 s** | Liveness and readiness are **off** until this passes once |
| liveness | 10 s | 3 s | 3 misses (~30 s) | Container restarted |
| readiness | 5 s | 3 s | 1 miss | Pod removed from `gateway` Service until it passes |

**Why `/health` never touches the database:** if it did, a 10-second Postgres blip would fail liveness on
**all 4** gateways at once, and Kubernetes would restart them together, turning a small outage into a full
one.

**Why the startup probe exists** (learned the hard way, section 33): without it, liveness judged pods from
second 0. When 4 pods booted at once on the busy node, they took longer than 30 s, and Kubernetes **killed
healthy pods mid-boot**. The startup probe gives up to 60 s to boot, then hands over to liveness for fast
detection.

**Why 3 s timeouts, not the default 1 s:** on a busy node, even a healthy process can take more than 1 s to
answer. The 1 s default caused both the gateway kills and Postgres/Redis flapping.

**Postgres and Redis** have readiness probes only (`pg_isready`, `redis-cli ping`), every 5 s, with a
**5 s** timeout, raised from the 1 s default after Postgres flapped out of its Service 68 times in 5 hours.

**Known limit:** readiness stays "ready" while Postgres is down, so requests still reach pods that will
answer 503. Upgrade path: a separate `/ready` endpoint that checks the DB pool, used only for readiness.

---

## 9. Resources: requests and limits

🟢 **Request** = "reserve me this much room". **Limit** = "never let me take more than this".

🔵
- The **scheduler** places pods using **requests** (a pod is `Pending` if no node has room for its requests).
- Going over the **memory limit** gets the container killed: `OOMKilled`, exit code 137.
- Going over a **CPU limit** only slows it down (throttling). I set no CPU limits on purpose, so pods can
  use idle CPU.

| Pod | CPU request | Memory request / limit |
|---|---|---|
| gateway (each of 4) | 100m | 128 Mi / 256 Mi |
| postgres | 100m | 256 Mi / 512 Mi |
| redis | 50m | 64 Mi / 128 Mi |
| Prometheus | 100m | 400 Mi / 1 Gi |
| Grafana | 50m | 256 Mi / **512 Mi** (was 256 Mi → OOMKilled, section 33) |
| Alertmanager | 10m | 32 Mi / 64 Mi |

**Lesson:** limits are guesses until you watch real use. When a pod restarts unexpectedly, check
`kubectl get pod … -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'` first. It says
`OOMKilled` or `Error`, which splits the problem in half immediately.

---

## 10. Postgres

🟢 The gateway's filing cabinet: tenants, API key hashes, limits, and one row per call.

🔵 Three objects in [postgres.yaml](../k8s/postgres.yaml):

| Object | Setting | Why |
|---|---|---|
| PVC `postgres-data` | 1 Gi, `ReadWriteOnce`, StorageClass `standard` | A disk that outlives the pod (section 14) |
| Deployment `postgres` | `postgres:17-alpine`, **major version pinned** | A new major changes the on-disk format and can't read the old data folder |
| | `strategy: Recreate` | Stop the old pod **before** starting the new one. A rolling update would briefly run two Postgres processes on the same data folder, which corrupts it. Cost: a few seconds of downtime per update |
| | `PGDATA=/var/lib/postgresql/data/pgdata` | A subfolder, because some disks have `lost+found` at their root and Postgres refuses to start in a non-empty folder |
| | `db-init` ConfigMap at `/docker-entrypoint-initdb.d` | Postgres runs these `.sql` files in name order, **only when the data folder is empty** |
| | readiness `pg_isready`, 5 s timeout | The Service only sends traffic once Postgres accepts connections |
| Service `postgres` | port 5432 | Stable address: `DATABASE_URL` says `@postgres:5432` |

**Schema setup (`db-init`):** [db-init.yaml](../k8s/db-init.yaml) is **generated** from `db/*.sql`:
```bash
kubectl create configmap db-init -n llm-gateway --from-file=db/ --dry-run=client -o yaml > k8s/db-init.yaml
```
**Known limit:** a future `006_*.sql` will **not** run on an existing database (the folder isn't empty).
When the first new migration lands, add a migration Job.

**In-cluster on purpose:** $0 and simple. With real data you'd use a managed database (backups, patching,
failover done for you).

---

## 11. Redis

🟢 The gateway's fast shared scoreboard: rate-limit "ticket jars" and monthly quota counters.

🔵 [redis.yaml](../k8s/redis.yaml): `redis:8-alpine`, 1 replica, Service `redis:6379`, and **no disk at
all** (`--save "" --appendonly no`).

**Why no disk:** every counter can be rebuilt from Postgres (real tokens + open quota holds, Project 2).
Losing Redis costs a short rebuild, not money. A disk would just be one more thing to break.

**What happens when Redis restarts:**
1. For a few seconds calls get `503 rate limiter unavailable` (fail closed).
2. Then Redis is empty: rate-limit jars start full, and each tenant's quota counter is rebuilt from Postgres
   on their next request.
3. Nobody gets free tokens, nobody gets blocked unfairly.

---

## 12. Config and Secrets

### 12.1 ConfigMap vs Secret

🟢 **ConfigMap** = a notice board anyone can read. **Secret** = a locked drawer.

🔵 Both become environment variables in the pod. The difference is **permissions and handling, not
encryption**: a Secret is only **base64-encoded** (reversible by anyone with `base64 -d`). The protection is
**who can read it** (RBAC, Kubernetes' permission system).

**Env vars are copied in when the container starts, then frozen.** If `gateway-config` changes in Git, Argo
CD updates the ConfigMap, but running pods keep the old values: the pod template didn't change, so there's no
rollout. Fix now: restart the Rollout (section 31). Proper fix later: Kustomize's `configMapGenerator`,
which puts a content hash in the ConfigMap's name.

### 12.2 Every secret in the system

| Secret | Namespace | Contains | Used by | Created by | If it leaks |
|---|---|---|---|---|---|
| `gateway-secrets` | llm-gateway | `UPSTREAM_API_KEY`, `DATABASE_URL`, `POSTGRES_PASSWORD` | gateway, postgres | Me, from `.env` + a random DB password | Someone spends my OpenRouter credit → rotate at OpenRouter |
| `ghcr-pull` | llm-gateway | GitHub classic PAT, **only** `read:packages` | kubelet (pulling the image) | Me, from a hidden prompt | Someone can pull my image (read my code). Delete the token on GitHub |
| `grafana-admin` | monitoring | Grafana admin user + random password | Grafana | Me, `openssl rand` | Someone can edit dashboards. Recreate it |
| `repo-ai-platform` | argocd | SSH **deploy key** (read-only) for the repo | Argo CD repo-server | Me | Read my code. Delete the key on GitHub |
| `argocd-initial-admin-secret` | argocd | Argo CD admin password | Me (UI login) | Argo CD install | Full control of what's deployed |
| `argocd-secret`, `argocd-redis`, `argocd-notifications-secret` | argocd | Argo CD internals | Argo CD | Argo CD install | — |
| Tenant API keys | — | Never stored. Postgres keeps SHA-256 hashes | Tenants | `create_tenant` prints once | A DB leak exposes no working keys |
| `GITHUB_TOKEN` | GitHub Actions | Push to GHCR + commit the tag | CI | GitHub, per job | Dies when the job ends |

**Rule:** secrets go from a terminal prompt straight into the system that needs them, **never** through Git,
chat, or Slack. Git history is forever. A key pasted anywhere is a leaked key, so it gets revoked and
replaced (which happened once, with the first GHCR token).

**Cost of this rule:** Argo CD can't recreate these on a fresh cluster. They're the manual steps in
section 30. The proper fix later is an external secret store (e.g. OCI Vault + External Secrets).

---

## 13. Networking: how `postgres` becomes a working connection

### Step 1: name → IP (CoreDNS)

Inside every pod, `/etc/resolv.conf`:
```
nameserver 10.96.0.10                                          ← CoreDNS's Service IP
search llm-gateway.svc.cluster.local svc.cluster.local cluster.local
```
The gateway asks for `postgres`. The `search` line makes it try `postgres.llm-gateway.svc.cluster.local`,
and CoreDNS answers `10.110.99.166`. A pod in another namespace would need `postgres.llm-gateway`.

### Step 2: Service IP → pod IP (kube-proxy)

🟢 A Service IP is like a company's main phone number: no desk has that number, and the switchboard forwards
each call to someone who's in today.

🔵 **Nothing listens on 10.110.99.166.** It's a **virtual IP**. kube-proxy writes network rules (iptables) on
the node: "connections to this IP:port → one of the **ready** pod IPs in the Service's EndpointSlice". For a
Service with several pods, the choice is **random per connection** (this is what splits canary traffic).

**A Service with no ready pods rejects connections immediately.** That's why "Postgres not ready yet"
shows up in the gateway as `Connection refused` to the **Service IP**, not as a timeout.

### Step 3: from my Mac: `kubectl port-forward`

```
browser/curl → localhost:8000 → kubectl (on the Mac) → API server → ONE pod :8000
```
- It's a **debugging tunnel**, not load balancing: it picks **one pod** when it starts and sticks to it.
- **If that pod is replaced, the tunnel dies** (this is why Grafana broke after its OOM restart). Restart the
  port-forward.
- `Connection refused` on `localhost` = the tunnel isn't running at all.

### Traffic leaving the cluster (to OpenRouter)

Pod → node → Colima VM → Mac → internet. Each hop rewrites the source address (NAT). On OKE the pods sit in
a **private** subnet and leave through the NAT gateway in `infra/cluster/network.tf`: out only, nothing can
connect in.

---

## 14. Storage: what "the disk survives" means

```
 PVC postgres-data (1Gi, "I need a disk")
   │  minikube's storage-provisioner sees an unbound claim
   ▼
 PV pvc-627bb417-… (the actual disk) = a folder on the minikube node
   │  bound 1:1 to the claim
   ▼
 mounted into the postgres pod at /var/lib/postgresql/data  (data in …/pgdata)
```

| I do this | Data survives? | Why |
|---|---|---|
| Delete the postgres **pod** | ✅ | A new pod mounts the same PVC |
| Update Postgres (Recreate) | ✅ | Same PVC, old pod stops first |
| Restart Colima / minikube | ✅ | The node's folders are kept |
| Delete the **namespace** | ❌ | PVC deleted → PV deleted (reclaim policy `Delete`) |
| `minikube delete` | ❌ | The whole node (and its folders) is gone |
| Remove `postgres.yaml` from Git | ❌ | Argo CD **prune** deletes the PVC |

**The only persistent disk in the whole cluster is Postgres's.** Redis has none on purpose. Prometheus has
none either, so its metrics are lost when its pod restarts (section 27).

On OKE the same PVC would create an **OCI Block Volume**, a real network disk.

---

## 15. Kubernetes under the hood: what happens on `apply`

### The control plane (`kube-system`)

| Pod | Simple words | What it does |
|---|---|---|
| `kube-apiserver` | The front desk | The **only** way in. kubectl, controllers, Argo CD all talk to it. It checks permissions and validates objects |
| `etcd` | The filing cabinet | Key-value database holding every object. Lose etcd = lose the cluster's memory |
| `kube-controller-manager` | The managers | Built-in loops: Deployment, ReplicaSet, EndpointSlice, PVC binding… |
| `kube-scheduler` | The seating planner | Picks a node for each new pod, based on its requests vs free room |
| `kube-proxy` | The switchboard | Programs node network rules so Service IPs reach pod IPs |
| `coredns` | The phone book | Answers "what's the IP of `postgres`?" |
| *(kubelet, not a pod)* | The hands | An agent on the node that starts containers and runs probes |

### From "Argo CD applies the Rollout" to "a pod serves traffic"

```
 Argo CD ──1──▶ API server ──2──▶ etcd: Rollout "gateway" (replicas 4, image :sha) stored
                    │
                    │ 3. Argo Rollouts controller (watching) sees a new pod template
                    │    → creates ReplicaSet gateway-7cc469c5ff (hash of the template), scales it (section 20)
                    │ 4. ReplicaSet controller: "want N pods, have fewer" → creates Pod objects (Pending)
                    │ 5. Scheduler: "needs 100m CPU + 128Mi, node minikube has room" → assigns the node
                    ▼
               kubelet on node minikube
                 6. image present? IfNotPresent → pull from GHCR with ghcr-pull if not
                 7. reads ConfigMap gateway-config + Secret gateway-secrets → env vars
                 8. starts the container as uid 10001 → uvicorn → lifespan (DB pool, Redis, metrics server :9100)
                 9. startup probe every 2 s → passes → liveness + readiness take over
                                    │
 10. EndpointSlice controller: pod Ready + label app=gateway → adds its IP to Service "gateway"
     → it starts receiving traffic
```

**Ownership chain** (each object has an `ownerReference` to its parent, which is how cleanup works):
```
 Rollout/gateway
   ├── ReplicaSet/gateway-7cc469c5ff   (current stable: 4 pods)
   │     ├── Pod/gateway-7cc469c5ff-9hw94
   │     ├── Pod/gateway-7cc469c5ff-bxth6
   │     ├── Pod/gateway-7cc469c5ff-rzv7j
   │     └── Pod/gateway-7cc469c5ff-zfq58
   └── ReplicaSet/gateway-b4fc699c8    (previous version: 0 pods, kept as history)
```

**Labels are the glue.** Nothing is linked by name: the Rollout, both Services, and the PodMonitor all find
the gateway pods by `app: gateway`. A typo in a label = a Service with no endpoints = "connection refused",
with every pod looking healthy.

### Startup order (and why it doesn't matter much)

Kubernetes starts everything at once, with no "wait for Postgres". After a cluster restart, the gateway
often boots before cluster DNS or Postgres is ready, fails in `lifespan`, exits, and gets restarted with a
growing delay (10 s, 20 s, 40 s… = `CrashLoopBackOff`). Within a minute everything is up. The cost is 1–3
harmless restarts on a cold start. That's normal Kubernetes: **crash and retry instead of careful ordering**.

---
---

# Part 3: Delivery (CI → Argo CD → Argo Rollouts)

## 16. The image

🟢 A sealed lunchbox: code + exact dependencies + Python, the same wherever it's opened.

🔵 [Dockerfile](../Dockerfile), two stages:

```
 1. Build context      the repo folder MINUS .dockerignore (.env, .venv, .git, infra, *.tfvars)
                                          │
 2. Stage "build"      python:3.12-slim + uv 0.11.7 (pinned)
                       COPY pyproject.toml uv.lock      ← lock files only
                       RUN uv sync --frozen --no-dev    ← creates /app/.venv (fails if the lock is stale)
                                          │
 3. Stage 2 (final)    fresh python:3.12-slim
                       useradd app (uid 10001)
                       COPY --from=build /app/.venv     ← only the result crosses over (no build tools)
                       COPY gateway/
                       USER 10001, EXPOSE 8000, CMD uvicorn gateway.main:app
```

| Choice | Why |
|---|---|
| Lock files copied **before** the code | Docker caches layers until the first changed one. Editing `gateway/*.py` rebuilds only the tiny code layer, not the dependency layer |
| Two stages | The shipped image has no uv or build tools: smaller, and fewer packages for the scanner to flag |
| Non-root UID 10001 | A break-in doesn't get root. The fixed number lets Kubernetes enforce `runAsNonRoot` |
| `.dockerignore` includes `.env` | **Security, not tidiness:** without it, the real API key is sent to the build and one careless `COPY . .` bakes it into a layer. Layers are permanent, and a later `rm` doesn't remove it |
| No config baked in | `DATABASE_URL` etc. come from env vars that Kubernetes injects |

**Size:** ~155 MB (it includes `prometheus-client` since Project 4).

---

## 17. CI: GitHub Actions

File: [.github/workflows/gateway.yml](../.github/workflows/gateway.yml).

### 17.1 When it runs

| Trigger | Runs? | Does |
|---|---|---|
| Push to `main` touching `gateway/**`, `Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock`, or the workflow | ✅ | Build → scan → push → commit tag |
| Pull request touching those | ✅ | Build → scan only (nothing unreviewed reaches the registry) |
| Push touching only `k8s/`, `argocd/`, `docs/` | ❌ | Nothing: no new image is needed. Argo CD applies `k8s/` directly |

### 17.2 The steps

```
checkout
   │
Build     docker build -t ghcr.io/musishere/ai-platform-engineering/gateway:$GITHUB_SHA .
   │
Scan      docker run aquasec/trivy:0.74.0 image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed <image>
   │        a fixable HIGH/CRITICAL vulnerability → job fails → nothing is pushed or deployed
   │  (main only from here)
Push      docker login ghcr.io with GITHUB_TOKEN; docker push <image>   ← the exact bytes that were scanned
   │
Deploy    sed the image line in k8s/gateway.yaml → commit "deploy: gateway <sha7>" → pull --rebase → push
```

### 17.3 Why each piece is built this way

| Choice | Why | Gave up |
|---|---|---|
| Scan **before** push | A vulnerable image never reaches the registry at all | — |
| Push the scanned image, not a rebuild | A rebuild could pull a newer, unscanned base image | — |
| `--ignore-unfixed` | A CVE with no fix yet can't be acted on; blocking on it would stop every deploy | Some known-but-unfixable CVEs ship (reported, not blocking) |
| Tag = git SHA | Every commit is a distinct, traceable image; it changes the pod template, so it triggers a rollout | — |
| CI commits the tag to Git | Git shows exactly what's deployed and when; rollback = `git revert` | Bot commits in history; I must `git pull --rebase` before pushing |
| No loop | GitHub never starts workflows from commits made with `GITHUB_TOKEN`, and the commit only touches `k8s/` | — |
| `concurrency` queued, not cancelled | Two quick merges can't race; an older build can't land last and roll the cluster back | Deploys queue |
| `permissions: contents: write, packages: write` only | Least privilege for the job's token | — |
| No layer cache | Simple (~1 min per build) | Add buildx with a GHA cache if builds get slow |
| amd64 only | minikube on an Intel Mac | Add arm64 when moving to OKE's ARM workers |

**CI never touches the cluster.** It has no cluster credentials. A leaked CI secret can push an image, but
can't change what runs until Argo CD pulls a Git change.

---

## 18. Argo CD: setup

### 18.1 Installed how

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml
```
- **v3.5.3, pinned.**
- **`--server-side`:** Argo CD's CRDs (custom resource definitions, which teach Kubernetes new kinds like
  `Application`) are too big for normal `kubectl apply`. It stores a copy of each object in an annotation
  limited to 256 KB. Server-side apply keeps that bookkeeping in the API server instead. The same fix was
  needed for Argo Rollouts, and inside the monitoring Application (`ServerSideApply=true`).

### 18.2 Access to the private repo

- A **read-only SSH deploy key** added to the GitHub repo, and its private half stored as the Secret
  `repo-ai-platform` in `argocd`, labelled `argocd.argoproj.io/secret-type=repository`.
- Argo CD checks GitHub's SSH host key, so a fake "github.com" can't feed it manifests.

### 18.3 The two Applications

An **Application** is Argo CD's "instruction card": *keep this destination identical to that source*.

| Application | Source | Destination | File |
|---|---|---|---|
| `gateway` | Git repo, `main`, path `k8s/` | namespace `llm-gateway` | [argocd/gateway.yaml](../argocd/gateway.yaml) |
| `monitoring` | Helm chart `kube-prometheus-stack` **91.8.2** from `prometheus-community.github.io/helm-charts` | namespace `monitoring` | [argocd/monitoring.yaml](../argocd/monitoring.yaml) |

Both use:
```yaml
syncPolicy:
  automated:
    prune: true      # a file removed from Git → its object is deleted from the cluster
    selfHeal: true   # a hand edit (kubectl edit) → reverted to what Git says
```

**The Applications themselves are applied by hand once** (`kubectl apply -f argocd/…`). That's the
bootstrap: after it, Git drives everything. Editing an `argocd/*.yaml` file needs a re-apply by hand. An "app
of apps" (an Application that syncs `argocd/`) would remove that step if apps multiply.

### 18.4 Its parts (pods in `argocd`)

| Pod | Simple words | Job |
|---|---|---|
| `argocd-repo-server` | The reader | Clones Git with the deploy key, renders `k8s/` and Helm charts into objects |
| `argocd-application-controller-0` | The thermostat | Compares desired vs live, applies differences, judges health |
| `argocd-server` | The dashboard | Web UI + API |
| `argocd-redis` | Its notepad | A cache for Argo CD. If it's down, Argo CD is slower, not broken |
| `argocd-dex-server`, `-applicationset-controller`, `-notifications-controller` | Extras | SSO, templated apps, notifications: unused |

---

## 19. Argo CD: how it works

### 19.1 One loop, step by step

```
                    ┌────────────── about every 3 min (or "Refresh", or a webhook) ──────────────┐
                    ▼                                                                             │
 1. repo-server: git fetch main over SSH (or download the Helm chart)                            │
 2. render → DESIRED objects (cached per commit SHA / chart version)                             │
 3. controller: LIVE objects ◀── it keeps a live watch on the cluster, updated instantly         │
 4. diff DESIRED vs LIVE → Synced / OutOfSync                                                    │
 5. OutOfSync + automated → apply the difference (Namespaces → ConfigMaps/Secrets → Services →   │
    workloads, in a safe order)                                                                  │
    selfHeal → also re-apply when LIVE drifted       prune → delete owned objects gone from Git  │
 6. health of every object → Healthy / Progressing / Degraded / Suspended / Missing ─────────────┘
```

### 19.2 Sync vs health: two separate questions

| | Question | Values |
|---|---|---|
| **Sync status** | Does the cluster match Git? | `Synced`, `OutOfSync` |
| **Health status** | Is what runs actually working? | `Healthy`, `Progressing`, `Suspended`, `Degraded`, `Missing` |

**Synced + Degraded** = "I applied exactly what Git says, and it's broken". That's a bad release, and the
fix belongs in Git. Argo CD alone doesn't protect you from bad code: it deploys it faithfully. That's what
the canary is for. Argo CD understands Rollout health: `Progressing` during steps, `Healthy` when done,
`Degraded` after an abort.

### 19.3 Why a hand edit is reverted in seconds, but a push takes minutes

- The **cluster side** is a **live watch**: a `kubectl edit` is seen immediately, and selfHeal reverts it.
- The **Git side** is **polled** every ~3 minutes (plus random jitter): Argo CD can't know a commit happened
  until it asks.
- To check Git **now**: `kubectl annotate application gateway -n argocd argocd.argoproj.io/refresh=normal --overwrite`
  (the same as the Refresh button).
- A GitHub **webhook** would make it instant, but needs Argo CD reachable from the internet, which a laptop
  cluster (or a private OKE cluster) isn't.

### 19.4 Which objects it owns (and why my Secrets are safe)

Every object Argo CD manages carries an annotation like:
```
argocd.argoproj.io/tracking-id: gateway:argoproj.io/Rollout:llm-gateway/gateway
```
**Prune only deletes objects with this annotation.** Hand-made Secrets (`gateway-secrets`, `ghcr-pull`,
`grafana-admin`) never get it, so they survive. **Risk:** deleting `postgres.yaml` from Git deletes the
database's PVC.

### 19.5 Rollback in GitOps

| Action | Sticks? | Why |
|---|---|---|
| `git revert <bad commit>` + push | ✅ | Git changes, Argo CD applies it |
| `kubectl rollout undo` / `kubectl edit` / `kubectl set image` | ❌ | selfHeal sees the cluster ≠ Git and puts Git's version back |

### 19.6 Helm charts through Argo CD (the monitoring app)

Argo CD renders the chart itself (`helm template`-style); there's no helm CLI and no `helm install`. Two
consequences:
1. **The chart version and every setting are in Git** (`targetRevision`, `valuesObject`).
2. **Anything random or cluster-dependent in a chart causes endless drift.** Helm's `lookup` function
   (read something from the live cluster) returns nothing under Argo CD. The Grafana chart used it to reuse
   its admin password, so every render invented a new random password → new checksum → Grafana restarted
   every 1–2 minutes. The fix: provide the password from our own Secret (`grafana.admin.existingSecret`).

---

## 20. Argo Rollouts: canary releases

### 20.1 Installed how

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts --server-side -f https://github.com/argoproj/argo-rollouts/releases/download/v1.10.0/install.yaml
```
v1.10.0, pinned. One controller pod. It must exist **before** Git mentions a `Rollout`, or Argo CD can't
apply that kind.

### 20.2 Stable vs canary

🟢 **Stable** = the version I already trust (the last one that reached 100%). **Canary** = the new version
on trial. **Both serve real callers at the same time**, the canary with a small share. If the canary passes,
it becomes the new stable. If it fails, it's removed and stable goes back to 100%.

🔵 Each version is its own **ReplicaSet**, named after a hash of its pod template. A release is the
controller turning two dials: stable pods down, canary pods up.

### 20.3 The steps (from [gateway.yaml](../k8s/gateway.yaml))

```yaml
strategy:
  canary:
    canaryService: gateway-canary
    maxUnavailable: 0        # never fewer than 4 serving pods during a release
    maxSurge: 1              # up to 1 extra pod while old and new overlap
    steps:
      - setWeight: 25        # 1 of 4 pods is the new version
      - analysis: {templates: [{templateName: gateway-smoke}]}
      - pause: {duration: 2m}
      - setWeight: 50
      - pause: {duration: 2m}
      # after the last step: 100%
```

### 20.4 How `setWeight` becomes traffic (two stages)

**Stage 1, Rollouts does math: weight → pod counts** (both sides rounded **up**):
```
canary pods = ceil(replicas × weight/100)        stable pods = ceil(replicas × (100−weight)/100)
setWeight 25 → canary ceil(1.0)=1, stable ceil(3.0)=3   → 1 of 4 = 25%
setWeight 50 → canary 2, stable 2                       → 50%
(10 replicas, 25% → canary ceil(2.5)=3, stable ceil(7.5)=8 → 11 pods, 3/11 ≈ 27%)
```
Rounding up means the canary is never 0 pods and the stable side never has less capacity than asked, at
the cost of a temporary extra pod. 4 replicas with steps of 25 and 50 give exact whole numbers.

**Stage 2, kube-proxy rolls the dice: pod counts → connections.** The `gateway` Service picks a ready pod
**at random per connection**. It knows nothing about versions; with 1 canary pod out of 4, about 1 in 4
connections lands there.

**Consequence: the split is per connection, not per request.** An SDK client keeps its connection open,
so one client sticks to one version. The percentage only holds on average across many connections. An
ingress with traffic weights would split per request (and allow 1%), at the cost of one more component.

### 20.5 The smoke test (analysis step)

[gateway-smoke.yaml](../k8s/gateway-smoke.yaml) is an **AnalysisTemplate**. At the analysis step:

```
Rollout → creates an AnalysisRun → creates a Job → a curl pod runs:
  5 × POST http://gateway-canary/v1/messages  with x-api-key: gw_smoke_test_not_a_real_key
  every answer must be 401
     exit 0 → Successful → next step
     exit 1 → Failed     → ABORT
```

**Why 401 is the right signal:** a bad key is looked up in Postgres, so a 401 proves the new code started,
parses requests, and **reached Postgres**. It costs $0 (it never reaches Claude) and writes no usage row. A
503 (DB unreachable) or a 500 (bug) fails it.

**Why it targets `gateway-canary`:** during a release, Rollouts adds the canary's hash to that Service's
selector, so all 5 requests hit **only the new pods**. Through `gateway` they'd hit the old pods 75% of the
time, and pass even with a broken canary.

**Why it's in its own file:** CI's `sed` rewrites **every** `image:` line in `gateway.yaml`, and would
overwrite the curl image.

### 20.6 A real release, as observed (2026-09-29)

| Time | Step | Pods (old / new) |
|---|---|---|
| 16:21:44 | Argo CD synced → `setWeight 25` | 4 → 3 / 1 |
| 16:21:57 | Canary ready in 13 s (0 restarts) → smoke test: 401 ×5 → **PASS** | 3 / 1 |
| 16:22:10 | `pause 2m` | 3 / 1 |
| 16:24:11 | `setWeight 50` | 2 / 2 |
| 16:24:18 | `pause 2m` | 2 / 2 |
| 16:26:18 | Promotion to 100% | 0 / 4 |
| 16:26:43 | **Healthy**: the new ReplicaSet is now stable | 0 / 4 |

That run showed capacity dropping to 3 serving pods at each step (the default `maxUnavailable: 25%`), which
led to `maxUnavailable: 0` (section 33).

### 20.7 When it fails

```
 v1 ✅✅✅✅  →  setWeight 25: v2 pod starts (v1 stays at 4, maxUnavailable 0)
             →  smoke test: HTTP 503 → FAIL
             →  ABORT: v2 scaled to 0, v1 back to 4   (blast radius: ~25% of connections, seconds)
 Argo CD: Synced + Degraded   (Git still says v2; the cluster protects callers)
 Fix: git revert (or a fix commit) + push → a fresh canary
```

**Known gap:** the smoke test runs **once**, at 25%. A bug that only shows on real traffic during the pauses
(e.g. 500s on streaming) isn't caught: pauses are timers, not checks. Upgrade path (Project 4): a
**background analysis** that queries Prometheus for the canary's error rate through every step.

---

## 21. One code push, end to end

The metrics release (commit `2e42873`, 2026-09-30) is the first to go through the **whole** chain from one
push:

```
 t+0      git push (gateway/metrics.py etc.)
 t+~3m    Actions: build → Trivy scan (incl. new prometheus-client) → push :2e42873… → bot commit 80e49a8
 t+≤3m    Argo CD polls Git, sees the new tag, applies the Rollout
 t+~5m    Rollouts: 25% → smoke test PASS → 2m → 50% → 2m → 100%
          new stable ReplicaSet gateway-7cc469c5ff, 4 pods
 total    ≈ 10 minutes, no human action after the push
```

**My only habit change:** `git pull --rebase` before my next push, because the bot commits to `main`.

---
---

# Part 4: Monitoring (Prometheus + Grafana)

## 22. Why monitoring, and the three kinds of signal

🟢 **The goal of Project 4:** know the gateway is unhealthy **before tenants notice**. Before this, the only
way to know how it was doing was to query Postgres by hand or read logs.

| Signal | Analogy | Answers | Tool here |
|---|---|---|---|
| **Metrics** | A car's dashboard gauges | "How many, how fast, how often?" Cheap numbers over time | **Prometheus + Grafana** ✅ |
| **Logs** | The driver's diary | "What exactly happened at 10:42?" | `kubectl logs` (for now) |
| **Traces** | A parcel's tracking history | "Where did *this* request spend its 8 seconds?" | OpenTelemetry (next step) |

Metrics come first: dashboards, SLOs and alerts are all built on them.

### How Prometheus collects: pull, not push

🟢 An **electricity meter** in each house only counts. A **meter reader** walks past every 15 seconds, reads
each dial, and writes the number in a notebook with the time.

🔵 Each gateway pod serves a plain-text page at `:9100/metrics`. Prometheus fetches it every 15 s (a
**scrape**) and stores each number with a timestamp.

**Why pull:** the gateway doesn't need to know where Prometheus is, or buffer and retry if it's down. And if a
scrape fails, Prometheus *knows* the pod is unreachable: its `up` metric becomes 0, which is itself an alarm.

---

## 23. What the gateway measures

### 23.1 The six metrics

| Metric | Type | Labels | Answers |
|---|---|---|---|
| `gateway_requests_total` | Counter | tenant, model, status | Request rate and error rate, per tenant |
| `gateway_request_duration_seconds` | Histogram | tenant, model | Latency p50/p95/p99 (a stream counts until its **last** byte) |
| `gateway_time_to_first_token_seconds` | Histogram | model | For streams: how long until words start appearing |
| `gateway_tokens_total` | Counter | tenant, model, direction (`input`/`output`) | Token usage per tenant |
| `gateway_limit_rejections_total` | Counter | tenant, reason (`rate_limit`/`monthly_quota`) | Who hits our limits, and which one |
| `gateway_requests_in_flight` | Gauge | — | How busy each pod is right now |

Plus `up{namespace="llm-gateway"}`, which Prometheus creates itself for every target: 1 = scraped fine,
0 = unreachable.

### 23.2 The three metric types

| Type | Analogy | Can | Used for |
|---|---|---|---|
| **Counter** | Odometer | Only go up (reset to 0 on restart) | Requests, tokens, rejections |
| **Gauge** | Speedometer | Go up and down | Requests in flight |
| **Histogram** | Sorting parcels into weight bins | Each bin only goes up | Durations |

**Counters are turned into rates by Prometheus, not the gateway:**
```
10:00:00  gateway_requests_total = 1000
10:05:00  gateway_requests_total = 1300     rate = (1300 − 1000) / 300 s = 1 request/second
```
A pod restart (every canary!) resets its counters to 0. `rate()` detects the drop and handles it, which is
why graphs always use `rate(...)`, never the raw number.

**Histograms give percentiles from bins:**
```
…_bucket{le="1"}    40      40 requests took ≤ 1 s
…_bucket{le="2.5"}  85      85 took ≤ 2.5 s (includes the 40)
…_bucket{le="5"}    97
…_bucket{le="+Inf"} 100     all 100
…_sum 210.5   …_count 100   (for the average)
```
For **p95**, the 95th request falls between `le="2.5"` (85) and `le="5"` (97), and Prometheus estimates
inside that bin (≈ 4.6 s). That's `histogram_quantile(0.95, …)`.

**My buckets:** `0.05 … 10, 20, 30, 60, 120` s for duration (the library's default stops at 10 s, but LLM
calls often take 10–60 s, so p99 would read "more than 10 s"). TTFT: `0.1 … 30` s.

### 23.3 Labels and the cardinality rule

🟢 Every distinct label combination is a separate line in the meter reader's notebook, kept for days. A few
tenants × a few models × a few status codes is fine. A label whose values **a caller can invent** grows the
notebook without limit until Prometheus runs out of memory.

🔵 The rules applied:
- **model:** comes from the caller's request body, so it's **filtered**: only models in the price table
  become labels; everything else is `"other"`.
- **tenant:** our own database id, only set after the key is verified. Bad-key requests are `"none"`.
- **status:** HTTP codes, a small fixed set.
- **Never:** request ids, user text, raw model strings.
- **Watch item:** `tenant` on the duration histogram = tenants × models × 13 buckets. Drop it there if
  tenants grow into the hundreds.

### 23.4 Why a separate port (9100)

`/metrics` shows **tenant ids and their usage**, which is business data. The gateway Services only forward
8000, so port 9100 is unreachable from outside the cluster (including a future public load balancer),
without having to remember to block anything. Prometheus runs inside the cluster and scrapes each pod's IP
on 9100 directly.

---

## 24. How the gateway produces its metrics (the code)

Everything is in [gateway/metrics.py](../gateway/metrics.py), plus a few calls from `main.py` and
`streaming.py`. Library: `prometheus-client` 0.26.0 (the official Python client).

### 24.1 The pieces

```
 gateway/main.py
   lifespan():   metrics.start_server()          → a tiny HTTP server on :9100, on its own thread
   app.add_middleware(metrics.MetricsMiddleware) → counts every /v1/messages request
   messages():   request.state.tenant_id = …     → tells the middleware who called
                 request.state.model = …         → and which model
                 metrics.record_rejection(…)     → at the two 429 spots (rate_limit / monthly_quota)
                 metrics.record_tokens(…)        → after metering has the real counts
 gateway/streaming.py (when a stream ends)
                 metrics.record_ttft(…)          → from the first content_block_delta
                 metrics.record_tokens(…)
```

### 24.2 The middleware: every request counted once, in one place

**Problem:** `/v1/messages` has 8 ways to end (401, 503 ×3, 429 ×2, 413, reply/stream), plus crashes. A
counter in each spot means 8 places to maintain and forget, and crashes can't be counted that way at all.

**Solution:** a raw **ASGI** middleware that wraps the whole route. ASGI is the interface between uvicorn and
FastAPI: the app sends a response as `http.response.start` (status + headers), then one or many
`http.response.body` messages.

```python
async def __call__(self, scope, receive, send):
    started = perf_counter(); status = 500; IN_FLIGHT.inc()
    async def send_and_watch(message):          # our own `send`, handed to the app
        if message["type"] == "http.response.start":
            status = message["status"]          # note the status…
        await send(message)                     # …pass everything through unchanged
    try:
        await self.app(scope, receive, send_and_watch)   # returns only after the LAST byte
    finally:                                    # success, crash, or a streaming caller who left
        IN_FLIGHT.dec()
        REQUESTS.labels(tenant, model, status).inc()
        DURATION.labels(tenant, model).observe(perf_counter() - started)
```

| Detail | Why |
|---|---|
| Raw ASGI, not `@app.middleware("http")` | FastAPI's version sees the response when **headers** go out, which for a 2-minute stream is second 1. Wrapping `send` stops the clock at the real last byte |
| `status = 500` default | If the app crashes before answering, the caller gets a 500, so that's what's counted |
| No `await` in `finally` | A cancelled task (caller disconnected) gets cancelled again at its next `await`. Plain `.inc()` calls can't be interrupted |
| Tenant/model via `request.state` | The route writes into `scope["state"]` as it learns them; the middleware reads it at the end. Tested with a real FastAPI app, not assumed |
| Only `/v1/messages` | `/health` is hit every few seconds by probes and would drown real traffic |

### 24.3 Tokens and time to first token

- `metering.finish()` returns `(input_tokens, output_tokens)`, the same counts that go to billing. The
  metric and the bill can't disagree. Split by direction because output costs 5× input for Haiku.
- `StreamUsage` records `perf_counter()` at the **first `content_block_delta`** (the first actual word),
  not at `message_start`, which arrives before the model writes anything.

### 24.4 How it was checked

1. A self-check in `metrics.py` (`uv run python -m gateway.metrics`): unknown model → `other`, crash → 500,
   `/health` not counted, in-flight back to 0.
2. A real FastAPI app with the middleware: tenant/model labels arrive, and a 0.6 s stream is timed ≥ 0.6 s.
3. The real Docker image serves all 6 metrics on `:9100/metrics`.
4. **In the cluster:** after sending traffic, Prometheus' numbers matched the client's output exactly (6 × 200,
   25 × 400, 8 × 429, 3 × 401, 108 input + 174 output tokens).

---

## 25. The monitoring stack: what's installed

### 25.1 kube-prometheus-stack

🟢 One package with everything needed to monitor a Kubernetes cluster, installed as a whole.

🔵 The Helm chart **kube-prometheus-stack 91.8.2**, installed by the Argo CD Application `monitoring`
([argocd/monitoring.yaml](../argocd/monitoring.yaml)) into namespace `monitoring`.

| Component | Pod | Version | Job |
|---|---|---|---|
| **Prometheus operator** | `monitoring-kube-prometheus-operator-…` | v0.94 | Watches PodMonitor / ServiceMonitor / PrometheusRule objects and writes Prometheus' config |
| **Prometheus** | `prometheus-monitoring-kube-prometheus-prometheus-0` | v3.15.0 | Scrapes targets every 15 s (30 s default for the stack's own targets), stores the time series, evaluates rules |
| **Grafana** | `monitoring-grafana-…` (3 containers) | 13.2.3 | Dashboards. Two sidecar containers auto-load dashboards and data sources from ConfigMaps |
| **Alertmanager** | `alertmanager-monitoring-kube-prometheus-alertmanager-0` | — | Receives firing alerts from Prometheus, groups them, routes them to people (step 5) |
| **kube-state-metrics** | `monitoring-kube-state-metrics-…` | — | Metrics *about Kubernetes objects*: pod restarts, Rollout/Deployment replicas, PVC status… |
| **node-exporter** | `monitoring-prometheus-node-exporter-…` | — | Metrics about the node: CPU, memory, disk, network |

**What the chart also installed:**
- **10 CRDs** (new Kubernetes kinds), including `PodMonitor`, `ServiceMonitor`, `PrometheusRule`,
  `Prometheus` and `Alertmanager`.
- **134 alerting rules + 86 recording rules** for Kubernetes health (e.g. "pod crash-looping", "node
  memory low").
- **25 ready-made dashboards** (Kubernetes compute per namespace/pod, node, Prometheus itself…).

### 25.2 The operator pattern (the key idea)

🟢 You don't edit Prometheus' config file. You create a small Kubernetes object that says "scrape these
pods", and a robot (the **operator**) rewrites Prometheus' config for you. It's the same idea as Argo
Rollouts: a controller that turns a custom object into real actions.

🔵
```
 PodMonitor "gateway" (in k8s/, deployed by Argo CD)
        │ watched by
        ▼
 Prometheus operator → regenerates the scrape config (a Secret) → config-reloader sidecar tells Prometheus
        │
        ▼
 Prometheus starts scraping the new targets (within ~1 min)
```

### 25.3 The settings I chose (in `argocd/monitoring.yaml`)

| Setting | Value | Why |
|---|---|---|
| `targetRevision` | `91.8.2` | Pinned: an upgrade changes Prometheus, Grafana and CRDs at once, so it should be a deliberate one-line change |
| `podMonitorSelectorNilUsesHelmValues` (+ serviceMonitor, rule) | `false` | By default this Prometheus only reads monitors labelled with its own Helm release. Off, so it reads the gateway's PodMonitor in `llm-gateway` too |
| `retention` | `3d` | Plenty for learning, keeps disk and RAM small |
| Prometheus resources | 400 Mi request / 1 Gi limit | Fits the node |
| `grafana.admin.existingSecret` | `grafana-admin` | Our own password Secret. Without it the chart made a random one on every render (restart loop, section 33) |
| Grafana resources | 256 Mi / **512 Mi** | 256 Mi was OOMKilled when the UI opened |
| Alertmanager resources | 32 Mi / 64 Mi | It's tiny |
| `kubeEtcd`, `kubeControllerManager`, `kubeScheduler`, `kubeProxy` | `enabled: false` | On minikube they only listen on localhost, so Prometheus can't reach them and they'd fire "target down" forever. On OKE, Oracle runs the control plane. Off, so every alert we see is real |
| `syncOptions: ServerSideApply=true` | | The chart's CRDs are too big for client-side apply |
| `syncOptions: CreateNamespace=true` | | Argo CD creates `monitoring` itself |

---

## 26. How Prometheus finds and scrapes the gateway

### 26.1 The PodMonitor

[k8s/gateway-podmonitor.yaml](../k8s/gateway-podmonitor.yaml):
```yaml
kind: PodMonitor
metadata: {name: gateway, namespace: llm-gateway}
spec:
  selector:
    matchLabels: {app: gateway}         # stable and canary pods alike
  podMetricsEndpoints:
    - port: metrics                     # the NAMED port in gateway.yaml, not a hard-coded 9100
      path: /metrics
      interval: 15s
```

**PodMonitor, not ServiceMonitor:** a ServiceMonitor scrapes through a Service's ports, but the gateway
Services deliberately expose only 8000. A PodMonitor scrapes each pod directly on 9100.

**Order dependency:** the `PodMonitor` kind only exists once the monitoring stack is installed. The stack
went in first, then the PodMonitor was pushed.

### 26.2 What Prometheus scrapes (all 14 target groups, all up)

| Target group | Targets | What it measures |
|---|---|---|
| `podMonitor/llm-gateway/gateway` | **4/4** | **My gateway pods** |
| `serviceMonitor/…/kubelet` (3 endpoints) | 3 | Container CPU/memory per pod (cAdvisor), probes, kubelet itself |
| `serviceMonitor/…/kube-state-metrics` | 1 | Kubernetes object state (restarts, replicas) |
| `serviceMonitor/…/node-exporter` | 1 | Node CPU, memory, disk |
| `serviceMonitor/…/apiserver` | 1 | The Kubernetes API server |
| `serviceMonitor/…/coredns` | 1 | Cluster DNS |
| `serviceMonitor/…/prometheus` (2) | 2 | Prometheus watching itself |
| `serviceMonitor/…/alertmanager` (2) | 2 | Alertmanager |
| `serviceMonitor/…/operator`, `…/grafana` | 2 | The operator and Grafana |

Check it any time:
```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
# http://localhost:9090 → Status → Targets
```

### 26.3 One scrape, step by step

```
every 15 s, for each of the 4 gateway pod IPs:
  Prometheus → GET http://10.244.0.x:9100/metrics  (pod IP directly, no Service)
  gateway's metrics thread answers with the text page (~200 lines)
  Prometheus adds labels: namespace, pod, container, job, instance
  stores each line as (series, timestamp, value)
  sets up{pod="gateway-…"} = 1   (0 if the GET failed)
```
The gateway's own labels (tenant, model, status) plus Prometheus' labels (pod…) identify each series. That's
why dashboards use `sum by (tenant)`: to add the 4 pods together.

---

## 27. How Prometheus stores data

- **A time-series database (TSDB).** Each series is a list of `(timestamp, value)` pairs. Recent data sits in
  memory (the "head"), older data is compacted into blocks on disk.
- **68,377 series** right now. Almost all come from Kubernetes itself (kubelet, kube-state-metrics); the
  gateway adds only a few hundred. Series count drives Prometheus' memory use, which is why cardinality
  matters.
- **Retention 3 days:** older data is deleted automatically.
- **⚠️ No persistent disk:** Prometheus stores its data in the pod's temporary storage (no PVC). **A Prometheus
  pod restart loses all history.** Fine for learning. Before real use, add a PVC via
  `prometheus.prometheusSpec.storageSpec` in `argocd/monitoring.yaml`.
- **Recording rules** (86 from the stack) pre-compute expensive queries on a schedule and store the results
  as new series, so dashboards and alerts stay fast. The SLO step will add some for the gateway.

---

## 28. Grafana and the gateway dashboard

### 28.1 Open it

```bash
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
# http://localhost:3000, user admin, password:
kubectl get secret grafana-admin -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d; echo
```
Then **Dashboards → LLM Gateway**. **Explore** (compass icon) runs any PromQL query ad hoc.

### 28.2 How the data source and dashboards get into Grafana

- **Data source:** the chart provisions "Prometheus" (uid `prometheus`) pointing at
  `http://monitoring-kube-prometheus-prometheus.monitoring:9090/`, as the default.
- **Dashboards:** a sidecar container in the Grafana pod watches **all namespaces** for ConfigMaps labelled
  `grafana_dashboard: "1"` and loads their JSON. That's how the stack's 25 dashboards and mine appear.

**Why the dashboard is a ConfigMap in Git** ([k8s/gateway-dashboard.yaml](../k8s/gateway-dashboard.yaml)),
not clicked together in the UI: it's versioned and reviewed, deployed by Argo CD, and **survives a rebuild**.
UI-only edits are lost when the Grafana pod restarts (it has no disk either). To change it: edit in the UI →
**Export → JSON** → paste into the ConfigMap → push.

### 28.3 The panels and their queries

A **Tenant** dropdown at the top (`label_values(gateway_requests_total, tenant)`, multi-select, default All)
filters every panel through `tenant=~"$tenant"`.

| Panel | PromQL | Reads as |
|---|---|---|
| Gateway pods up | `sum(up{namespace="llm-gateway", pod=~"gateway-.*"})` | Should be 4 |
| Requests / s | `sum(rate(gateway_requests_total{tenant=~"$tenant"}[5m]))` | Traffic, averaged over 5 min |
| Error rate (5xx) | `100 * sum(rate(…{status=~"5.."}[5m])) / sum(rate(…[5m]))` | % of requests failing on our side or the provider's |
| In flight | `sum(gateway_requests_in_flight)` | Rising steadily = requests piling up |
| Request rate by tenant and status | `sum by (tenant, status) (rate(…[5m]))` | Who calls, and what they get back |
| Error rate by tenant | the error query, `by (tenant)` | Is one tenant failing when others aren't? |
| Latency p50/p95/p99 | `histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket[5m])))` | Typical vs slow tail |
| Time to first token p50/p95 | the same on `gateway_time_to_first_token_seconds_bucket` | Blank-screen time for streams |
| Tokens / min by tenant | `60 * sum by (tenant, direction) (rate(gateway_tokens_total[5m]))` | Input vs output use |
| Refused by our limits | `60 * sum by (tenant, reason) (rate(gateway_limit_rejections_total[5m]))` | rate_limit vs monthly_quota |

**Why the error rate counts only 5xx:** 4xx (bad key, bad request, our own 429s) are caused by the caller or
a limit working as designed. Counting them would make "error rate" jump every time a tenant misuses the API,
and paging someone for that would be noise. This definition is the basis for the SLO step.

**Why `[5m]` and `sum by`:** `rate(…[5m])` smooths over 5 minutes (and handles counter resets); `sum by
(tenant)` adds up the 4 pods, which each count separately.

### 28.4 Sending traffic to see it

```bash
kubectl port-forward -n llm-gateway svc/gateway 8000:80                     # terminal 1
export GATEWAY_API_KEY=$(kubectl exec -n llm-gateway svc/gateway -- \
  python -m gateway.create_tenant demo-$(date +%s) | tail -1)              # key never printed
uv run python scripts/client.py                                             # ~1 cent: 200s, a stream, a 401, a 400
for i in $(seq 30); do curl -s -o /dev/null -w "%{http_code} " -X POST localhost:8000/v1/messages \
  -H "x-api-key: $GATEWAY_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"no-such-model","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}'; done; echo
# → ~21 × 400 then 429s (the tenant's 20-ticket jar + 1/s refill). Free: the provider rejects the bad model.
```

---

## 29. Alerting (what exists so far)

```
Prometheus evaluates alert rules every 30 s → firing alerts → Alertmanager → groups, silences, routes
                                                                              → (no receivers configured yet)
```

- **134 alerting rules** from the stack cover Kubernetes health (crash loops, node pressure, Prometheus
  problems…).
- **`Watchdog` is always firing, on purpose.** It's a "dead man's switch": if it ever *stops* arriving
  somewhere, the alerting pipeline itself is broken.
- **No receivers yet:** alerts are visible in Alertmanager/Prometheus but not sent anywhere.
- **Next (Project 4, step 5):** SLOs for the gateway (e.g. "99% of requests succeed") and **burn-rate
  alerts** on them, written as a `PrometheusRule` in `k8s/`, next to the gateway.

---
---

# Part 5: Operating it

## 30. Rebuild everything from zero

Everything in Git rebuilds itself. These steps don't, on purpose: they're secrets, or the thing that starts
the robot.

```bash
# 1. The boxes
colima start --cpu 4 --memory 6
minikube start --driver=docker --cpus=4 --memory=5g

# 2. Argo CD (pinned; --server-side because its CRDs are too big)
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml

# 3. Argo Rollouts (pinned; must exist before Git mentions a Rollout)
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts --server-side -f https://github.com/argoproj/argo-rollouts/releases/download/v1.10.0/install.yaml

# 4. Argo CD's access to the private repo (read-only deploy key)
kubectl create secret generic repo-ai-platform -n argocd --from-literal=type=git \
  --from-literal=url=git@github.com:musishere/AI-platform-engineering.git \
  --from-file=sshPrivateKey=$HOME/.ssh/argocd_ai_platform
kubectl label secret repo-ai-platform -n argocd argocd.argoproj.io/secret-type=repository

# 5. Monitoring stack first (the gateway's PodMonitor needs its CRDs)
kubectl create namespace monitoring
kubectl create secret generic grafana-admin -n monitoring \
  --from-literal=admin-user=admin --from-literal=admin-password="$(openssl rand -hex 16)"
kubectl apply -f argocd/monitoring.yaml
#    wait until: kubectl get application monitoring -n argocd  → Synced Healthy

# 6. The gateway's secrets (never in Git)
kubectl apply -f k8s/00-namespace.yaml
PW=$(openssl rand -hex 16); KEY=$(grep '^UPSTREAM_API_KEY=' .env | cut -d= -f2-)
kubectl create secret generic gateway-secrets -n llm-gateway \
  --from-literal=POSTGRES_PASSWORD="$PW" \
  --from-literal=DATABASE_URL="postgresql://gateway:$PW@postgres:5432/gateway" \
  --from-literal=UPSTREAM_API_KEY="$KEY"
read -s "PAT?GHCR token (read:packages): "; echo
kubectl create secret docker-registry ghcr-pull -n llm-gateway \
  --docker-server=ghcr.io --docker-username=musishere --docker-password="$PAT"; unset PAT

# 7. The bootstrap: from here on, Git drives
kubectl apply -f argocd/gateway.yaml

# 8. A tenant (new database = no tenants)
kubectl exec -n llm-gateway svc/gateway -- python -m gateway.create_tenant <name>
```

**On OKE,** step 1 becomes `terraform apply` in `infra/cluster`. "Terraform builds the building, GitOps
arranges the furniture."

---

## 31. Command cheat sheet

```bash
# ── Boxes ────────────────────────────────────────────────────────────────────
colima start --cpu 4 --memory 6 && minikube start
minikube stop                                          # free RAM; the cluster is kept

# ── Look ─────────────────────────────────────────────────────────────────────
kubectl get pods -A                                    # everything
kubectl get pods -n llm-gateway -w                     # watch the app
kubectl logs -n llm-gateway -l app=gateway --prefix -f # all gateway pods
kubectl describe pod -n llm-gateway <pod>              # the Events section explains most failures
kubectl get pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'

# ── Use the gateway ──────────────────────────────────────────────────────────
kubectl port-forward -n llm-gateway svc/gateway 8000:80
kubectl exec -n llm-gateway svc/gateway -- python -m gateway.create_tenant <name>

# ── Argo CD ──────────────────────────────────────────────────────────────────
kubectl get applications -n argocd                     # sync + health of both apps
kubectl annotate application gateway -n argocd argocd.argoproj.io/refresh=normal --overwrite   # check Git now
kubectl port-forward -n argocd svc/argocd-server 8080:443       # UI: https://localhost:8080, user admin
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d; echo

# ── Canary ───────────────────────────────────────────────────────────────────
kubectl get rollout gateway -n llm-gateway -w          # Progressing / Paused / Healthy / Degraded
kubectl describe rollout gateway -n llm-gateway        # current step, why it aborted
kubectl get analysisrun,job -n llm-gateway             # smoke tests
kubectl logs -n llm-gateway job/<smoke-job>            # the 5 requests and PASS/FAIL
kubectl patch rollout gateway -n llm-gateway --type merge \
  -p "{\"spec\":{\"restartAt\":\"$(date -u +%FT%TZ)\"}}"   # restart all gateway pods
# optional plugin (one file, no brew):
#   curl -Lo /usr/local/bin/kubectl-argo-rollouts https://github.com/argoproj/argo-rollouts/releases/download/v1.10.0/kubectl-argo-rollouts-darwin-amd64
#   kubectl argo rollouts get rollout gateway -n llm-gateway --watch | abort | promote --full

# ── Monitoring ───────────────────────────────────────────────────────────────
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
kubectl get secret grafana-admin -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090   # Prometheus UI
kubectl port-forward -n llm-gateway <gateway-pod> 9100:9100 && curl -s localhost:9100/metrics | grep ^gateway_

# ── Regenerate the DB init ConfigMap after changing db/ ─────────────────────
kubectl create configmap db-init -n llm-gateway --from-file=db/ --dry-run=client -o yaml > k8s/db-init.yaml

# ── Clean slate (deletes the Postgres data too) ─────────────────────────────
kubectl delete namespace llm-gateway
```

---

## 32. Troubleshooting

**First rule:** read the **last** "caused by" error, then find the **first hop** that failed.

| Symptom | Likely cause | First check |
|---|---|---|
| `Connection refused` on `localhost:…` | The port-forward isn't running (or its pod was replaced) | `lsof -nP -iTCP:8000 -sTCP:LISTEN`; restart the port-forward |
| Grafana "failed to load its application files" | Grafana restarted mid-load (e.g. OOMKilled) and the tunnel died | `lastState.terminated.reason`; restart the port-forward |
| Pod `Pending` | Not enough CPU/memory requests left on the node | `kubectl describe pod` → `Insufficient memory` |
| `ImagePullBackOff` | `ghcr-pull` missing, token expired, or wrong tag | `kubectl describe pod` → Events |
| `CreateContainerConfigError` | A referenced Secret/ConfigMap doesn't exist | `kubectl get secret -n <ns>` |
| `CrashLoopBackOff` right after a cluster start | Booted before DNS/Postgres was ready | `kubectl logs --previous`; usually settles by itself |
| `OOMKilled` (exit 137) | Memory limit too low | Raise the limit in Git |
| Pods killed with "failed liveness probe" during boot | Slow boot on a busy node | The startup probe (already added) |
| `connection refused` to a **Service IP** from inside a pod | That Service has no ready pods | `kubectl get endpointslice -n <ns>` |
| Push made, nothing happens for minutes | Argo CD polls every ~3 min | The refresh annotation |
| Argo CD `OutOfSync` forever / constant restarts of a Helm-installed pod | Random or `lookup`-based values in a chart | Diff two ReplicaSets' pod templates |
| Rollout `Degraded` | The smoke test failed | `kubectl logs job/<smoke-job>`; fix in Git |
| 401 on every call | New database = no tenants, or a local `.env` key used against the cluster | Create a tenant in the cluster |
| Dashboard "No data" | No traffic yet, or the PodMonitor isn't picked up | `up{namespace="llm-gateway"}` in Explore |

---

## 33. What broke while building this, and the fixes

| # | Problem | Root cause | Fix |
|---|---|---|---|
| 1 | Code changes didn't deploy by themselves | Tag `:dev` never changed, so no rollout | CI with SHA tags + tag commit |
| 2 | The cluster couldn't pull the image | GHCR image private, no login | `ghcr-pull` read-only token |
| 3 | A token was pasted into chat | — | Treated as leaked: revoked, recreated via hidden prompt |
| 4 | New gateway pod: "connection refused" to Postgres | Postgres readiness timeout 1 s → flapped out of its Service under load (68× in 5 h) | `timeoutSeconds: 5` on Postgres + Redis |
| 5 | Buggy versions would reach 100% | Rolling update only checks `/health` | Argo Rollouts canary + smoke test |
| 6 | Argo Rollouts / Argo CD / chart CRDs failed to apply | CRDs > 256 KB annotation limit | Server-side apply |
| 7 | CI would overwrite the smoke test's image | `sed` rewrites every `image:` line in `gateway.yaml` | Smoke test in its own file |
| 8 | All 4 gateway pods killed while booting | Liveness judged from second 0, 1 s timeout | Startup probe + 3 s timeouts |
| 9 | Capacity dropped to 3 pods during canary steps | Default `maxUnavailable: 25%` | `maxUnavailable: 0`, `maxSurge: 1` |
| 10 | Monitoring "stuck" at Init | Just a slow first pull (Prometheus image 265 MB) | None needed |
| 11 | Argo CD's Redis "connection refused" | Its init container hung after a cluster restart | Deleted the pod; a fresh one started |
| 12 | Grafana restarted every 1–2 min | Chart generated a random password on every render (`lookup` doesn't work under Argo CD) → checksum changed | `grafana-admin` Secret + `existingSecret` |
| 13 | Grafana UI failed to load | Grafana OOMKilled at 256 Mi when the UI opened | Limit 512 Mi |
| 14 | Client got "connection refused" | The gateway port-forward wasn't running | Start it (3 terminals: Grafana, gateway, commands) |

**Pattern worth remembering:** 4, 8 and 13 were all **default or guessed limits** (1 s probe timeouts, a
256 Mi memory limit) that looked fine idle and failed under load. Watch real behaviour, then set limits.

---

## 34. Decisions and tradeoffs (interview stories)

| Decision | Chose | Gave up | Other option wins when |
|---|---|---|---|
| Local cluster | minikube ($0) | Real cloud networking, LBs, Workload Identity | Budget exists (OKE) |
| Manifests | Plain YAML in `k8s/` | Templating | Several environments → Kustomize overlays |
| Databases | In-cluster Postgres + Redis | Backups, HA, patching | Any real data → managed DB |
| Postgres controller | Deployment + PVC + Recreate | StatefulSet identity | Primary + standby replicas |
| Redis storage | None | Counters rebuilt after restart | Data that exists nowhere else |
| Secrets | Hand-created, never in Git | Full GitOps (manual rebuild steps) | Teams / many clusters → External Secrets + a vault |
| Image tags | Git SHA, committed by CI | Bot commits in history | — |
| Deploys | Argo CD pull-based, prune + selfHeal | Quick manual fixes (reverted) | Tiny projects where push-based is simpler |
| Releases | Canary by pod count, 4 replicas | Exact % and per-request split | High traffic / 1% canaries → ingress weights |
| Canary check | Smoke test (fake key → 401), $0 | Catching real-traffic errors | Metrics-based analysis (Project 4) |
| Probes | Startup + liveness + readiness on `/health` (no DB) | Readiness blind to DB outages | Add `/ready` once traffic matters |
| Metrics port | Separate 9100, PodMonitor | One more port | — (metrics are business data) |
| Monitoring install | kube-prometheus-stack via Argo CD | Weight (~1–1.5 GB), chart quirks | Very small setups → plain Prometheus |
| Prometheus storage | Temporary (no PVC), 3 d | History on restart | Any real use → PVC |
| Dashboard | ConfigMap JSON in Git | Click-and-save convenience | — |
| Error rate | 5xx only | Visibility of client misuse (still visible by status) | — |

---

## 35. Known limits

| Limit | Impact | Upgrade path |
|---|---|---|
| Smoke test runs once, at 25% | Real-traffic bugs during the pauses reach 100% | Prometheus-based background analysis |
| Canary split per connection | Few clients → uneven split | Ingress with traffic weights |
| Readiness ignores DB health | Requests reach pods that return 503 | `/ready` endpoint |
| `db-init` only runs on an empty DB; the ConfigMap is a copy of `db/` | New migrations don't apply; copy can drift | Migration Job + CI check |
| Prometheus has no disk | History lost on its restart | `storageSpec` PVC |
| Grafana has no disk | UI-only dashboard edits lost | Keep dashboards as ConfigMaps (already) |
| Secrets outside Git | Manual steps on rebuild | External Secrets + OCI Vault |
| `/metrics` readable by any pod in the cluster | Tenant usage visible inside the cluster | NetworkPolicy allowing only Prometheus |
| Argo CD polls Git | Up to ~3 min deploy lag | Webhook (needs a public Argo CD) |
| No alert receivers | Alerts go nowhere | Alertmanager receivers (SLO step) |
| `argocd/*.yaml` applied by hand | Edits need a manual re-apply | App of apps |

---

## 36. Later: minikube → OKE

```
                    minikube (now, $0)                    OKE (when funded)
                    ──────────────────                    ─────────────────
 cluster created by minikube start                        terraform apply (infra/cluster)
 nodes              1 container in Colima (amd64)         2 ARM VMs in a private subnet
 image              GHCR (amd64)                          GHCR, multi-arch (add arm64 in CI)
 public access      kubectl port-forward                  Service type LoadBalancer → OCI LB (public LB subnet)
 Postgres disk      folder on the node                    OCI Block Volume
 cloud permissions  none                                  Workload Identity (no static keys)
 control-plane metrics  disabled (localhost only)         not scrapeable (Oracle runs it)
 k8s/*.yaml         ──────────────── mostly the SAME files ────────────────
 Argo CD            points at minikube                    points at OKE (same Git repo)
```

The bottom rows are the payoff of GitOps: the deploy definitions don't care which cluster runs them.

---

## 37. Check yourself

**Q1.** I push a change to `gateway/limits.py`. List, in order, every system that acts on it until new pods
serve traffic, and what each one does.
<details><summary>Answer</summary>
GitHub Actions (paths filter matches) → build → Trivy scan → push to GHCR with the SHA tag → bot commits the
tag into k8s/gateway.yaml → Argo CD polls Git (≤ 3 min) and applies the new Rollout spec → Argo Rollouts
creates a new ReplicaSet and runs the steps (25% → smoke test → 2 m → 50% → 2 m → 100%) → the kubelet pulls
the image with ghcr-pull and starts pods → the startup probe passes → readiness passes → the EndpointSlice
adds them to the gateway Service. See 4, 17–21.
</details>

**Q2.** I push a change to `k8s/config.yaml` only. Does CI run? Do the gateway pods get the new value?
<details><summary>Answer</summary>
CI doesn't run (only image inputs trigger it). Argo CD updates the ConfigMap, but the pods keep the old env
vars: they're copied in at container start, and the pod template didn't change, so there's no rollout.
Restart the Rollout. See 12.1, 17.1.
</details>

**Q3.** Someone runs `kubectl set image` on the gateway to roll back quickly. What happens?
<details><summary>Answer</summary>
Argo CD's live watch sees the cluster differ from Git, and selfHeal puts Git's image back within seconds.
The rollback that sticks is `git revert` + push. See 19.5.
</details>

**Q4.** With 4 replicas and `setWeight: 25`, how many canary pods run, and why does the smoke test use
`gateway-canary` instead of `gateway`?
<details><summary>Answer</summary>
ceil(4 × 0.25) = 1 canary pod (3 stable). `gateway` selects all 4 pods, so the test would hit the old
version 75% of the time and pass even with a broken canary. `gateway-canary` gets the canary's hash added to
its selector, so it reaches only the new pod. See 20.4, 20.5.
</details>

**Q5.** Why does `/health` not check Postgres, and what's the downside?
<details><summary>Answer</summary>
If it did, a Postgres blip would fail liveness on all 4 gateways at once, and they'd all be restarted
together, turning a small outage into a full one. Downside: readiness stays "ready" while Postgres is down,
so requests reach pods that answer 503. See 8.
</details>

**Q6.** Grafana shows "failed to load its application files". How do you find the cause in two commands?
<details><summary>Answer</summary>
`kubectl get pods -n monitoring` (RESTARTS > 0 recently?), then
`kubectl get pod <grafana-pod> -n monitoring -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'`.
Here it said OOMKilled: Grafana died mid-page-load, and the port-forward died with the pod. See 9, 13, 33.
</details>

**Q7.** Why is `gateway_requests_total` graphed as `rate(...[5m])` and not as its raw value?
<details><summary>Answer</summary>
It's a counter that only goes up and resets to 0 when a pod restarts (every canary release). The raw value
shows meaningless cliffs; `rate()` turns it into requests per second and handles resets. See 23.2.
</details>

**Q8.** Why is the gateway scraped by a PodMonitor on port 9100 instead of a ServiceMonitor through the
`gateway` Service?
<details><summary>Answer</summary>
The Services deliberately expose only port 8000, so tenant usage data can never leave through them (or a
future public load balancer). A PodMonitor scrapes each pod's 9100 directly, inside the cluster. See 23.4, 26.1.
</details>

**Q9.** A caller sends 1,000 requests, each with a different made-up model name. What happens to
Prometheus, and why?
<details><summary>Answer</summary>
Nothing bad: the model label is filtered to known models, and all 1,000 become model="other", a single
series per tenant/status. Without the filter, each name would create new time series and could exhaust
Prometheus' memory (cardinality). See 23.3.
</details>

**Q10.** The Prometheus pod restarts. What do you lose, and what do you keep?
<details><summary>Answer</summary>
You lose all stored metric history (no PVC). You keep the configuration (it comes from the operator and
Git), the dashboards (ConfigMaps), and the gateway's own counters, which live in the gateway pods and are
scraped again. See 27.
</details>

---

## 38. Key terms

| Simple words | Technical term | One line |
|---|---|---|
| Sealed lunchbox | Image | Code + dependencies + runtime, built once, runs anywhere |
| Folder in the cluster | Namespace | Name scope + unit of cleanup and permissions |
| A running lunchbox | Pod | 1+ containers sharing a network address |
| Group of identical pods | ReplicaSet | One per version; Deployments and Rollouts manage them |
| Manager with a release plan | Rollout (Argo Rollouts) | Like a Deployment, plus canary steps and analysis |
| Phone number that never changes | Service | Stable DNS name + virtual IP in front of ready pods |
| Notice board / locked drawer | ConfigMap / Secret | Settings as env vars. Secret ≠ encrypted |
| Plugged-in hard drive | PersistentVolumeClaim | Storage that outlives the pod |
| Three health questions | Startup / liveness / readiness probe | Wait / restart / stop sending traffic |
| Reserve / ceiling | Request / limit | Used for scheduling / exceeding memory = OOMKilled |
| Thermostat | Control loop / reconciliation | Observe → compare → act, forever |
| Robot that makes the cluster match Git | Argo CD (GitOps) | Pull-based delivery with drift correction |
| Taste test before serving everyone | Canary release | New version gets a small share first, then grows or aborts |
| Taster | AnalysisTemplate / AnalysisRun | The check that decides promote or abort |
| New Kubernetes kind | CRD | Custom resource definition (Rollout, PodMonitor…) |
| Robot that configures an app from objects | Operator | A controller for custom resources (e.g. the Prometheus operator) |
| Meter reader | Scrape | Prometheus fetching `/metrics` on a schedule |
| Odometer / speedometer / weight bins | Counter / gauge / histogram | Metric types |
| Lines in the notebook | Time series / cardinality | One per label combination; too many = out of memory |
| "Scrape these pods" note | PodMonitor | Tells the Prometheus operator what to scrape |
| Dashboard auto-loader | Grafana sidecar | Loads ConfigMaps labelled `grafana_dashboard: "1"` |
| Dead man's switch | Watchdog alert | Always firing; silence means alerting is broken |
