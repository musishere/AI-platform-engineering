# Deployment: how the gateway runs on Kubernetes

> **Status (2026-09-29):** Steps 1–3 done: Argo CD deploys `k8s/` from `main` onto minikube (auto-sync, prune, selfHeal), and a real call was metered end to end. Step 4 done: a push to `main` builds, Trivy-scans and pushes `ghcr.io/…/gateway:<sha>`, CI commits the tag into `k8s/gateway.yaml`, and Argo CD rolls it out (first run 2026-09-29, ~5 min push → new pods). Step 5 (canary) is next.
> The OKE deploy is paused until funded, so everything below runs on **minikube on my Mac, for $0**.
> Moving to OKE later changes the "outside" of this picture, not the inside (see section 9).

**One-sentence version:** my gateway code is packed into an **image**, Kubernetes runs **2 copies** of it
next to a **Postgres** and a **Redis**, and a **Service** gives them one stable address. Later, Git becomes
the single source of truth: CI builds the image and Argo CD makes the cluster match what Git says.

**How to read this:** Part 1 (sections 1–12) is the **map**: what exists and why. Part 2 (sections 13–22)
is **under the hood**: what actually happens, step by step, with real output from my cluster.

---

## 1. The nested boxes: where everything physically runs

🟢 **In simple words:** a set of Russian dolls. My Mac holds a small Linux computer (Colima), which holds
a pretend Kubernetes computer (minikube), which holds my apps. Each doll can never be bigger than the one
it sits inside.

```
┌─ MacBook (16 GB RAM, 4 cores, macOS) ──────────────────────────────────────┐
│                                                                            │
│  terminal: kubectl, docker, minikube  ──────────┐  (talk to the boxes)    │
│                                                 ▼                          │
│  ┌─ Colima VM (Linux, 4 CPU, 6 GB) ───────────────────────────────────┐   │
│  │   runs the Docker engine                                           │   │
│  │                                                                    │   │
│  │   ┌─ container "minikube" (4 CPU, 5 GB) = ONE Kubernetes node ──┐  │   │
│  │   │   control plane: API server, scheduler, etcd                │  │   │
│  │   │   kubelet + container runtime                               │  │   │
│  │   │                                                             │  │   │
│  │   │   ┌─ namespace: llm-gateway ─────────────────────────────┐  │  │   │
│  │   │   │  gateway pod ×2    postgres pod    redis pod         │  │  │   │
│  │   │   └──────────────────────────────────────────────────────┘  │  │   │
│  │   └─────────────────────────────────────────────────────────────┘  │   │
│  └────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────┘
```

🔵 **Technically:** macOS can't run Linux containers natively, so Colima runs a Linux VM with the Docker
engine inside. Minikube's `docker` driver runs the whole Kubernetes node as **one Docker container** in
that VM. The control plane and my pods share that one node.

**Why Colima had to grow from 2 GB → 6 GB:** Kubernetes itself (~0.7 GB) + Argo CD (~0.5–1 GB) + Postgres +
Redis + 2 gateways add up to ~2 GB before any load. Squeezed into 2 GB, pods would sit in `Pending` (no room
to schedule them) or get `OOMKilled` (killed for running out of memory), and it would look like a bug in my
YAML.

---

## 2. What runs inside the cluster

🟢 **In simple words:** each app has a **manager** (Deployment) that keeps the right number of copies
running, and a **phone number** (Service) that stays the same even when copies are replaced. Settings come
from a **notice board** (ConfigMap) and a **locked drawer** (Secret). Postgres gets a **hard drive** (volume)
that survives restarts.

```
 namespace: llm-gateway
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                                                                              │
 │  ConfigMap gateway-config          Secret gateway-secrets                    │
 │  (not secret)                      (secret, NOT in Git)                      │
 │   UPSTREAM_BASE_URL                 UPSTREAM_API_KEY                         │
 │   REDIS_URL                         DATABASE_URL  (has the DB password)      │
 │        │                            POSTGRES_PASSWORD                        │
 │        └──────────── env vars ─────────┬──────────────────────┐              │
 │                                        ▼                      ▼              │
 │  Service "gateway" :80        Deployment "gateway"   Deployment "postgres"   │
 │   picks pods labelled   ───▶   replicas: 2            replicas: 1            │
 │   app=gateway                  ┌──────────┐           strategy: Recreate     │
 │                                │ pod  A   │           ┌──────────┐           │
 │                                │ :8000    │──┐        │ pod      │◀── Service│
 │                                └──────────┘  │ SQL    │ :5432    │  postgres │
 │                                ┌──────────┐  ├──────▶ │          │           │
 │                                │ pod  B   │  │        └────┬─────┘           │
 │                                │ :8000    │──┤             │ mounts          │
 │                                └──────────┘  │        ┌────▼──────────────┐  │
 │                                              │        │ PVC postgres-data │  │
 │                                              │        │ (disk, survives   │  │
 │  ConfigMap db-init ─── first start only ─────┼──────▶ │  pod restarts)    │  │
 │  (001…005_*.sql)                             │        └───────────────────┘  │
 │                                              │                               │
 │                                              │        Deployment "redis"     │
 │                                              └──────▶ ┌──────────┐◀── Service│
 │                                               counters│ pod :6379│   redis   │
 │                                                       │ NO disk  │           │
 │                                                       └──────────┘           │
 └──────────────────────────────────────────────────────────────────────────────┘
```

| Object | Count | What it's for |
|---|---|---|
| Namespace `llm-gateway` | 1 | A folder for everything, so `kubectl delete ns llm-gateway` cleans up in one go |
| Deployment + Service `gateway` | 2 pods | The FastAPI app from `Dockerfile` |
| Deployment + Service `postgres` | 1 pod | Tenants, usage events, holds |
| PersistentVolumeClaim `postgres-data` | 1 | Postgres's disk |
| Deployment + Service `redis` | 1 pod | Rate-limit tickets + monthly quota counters |
| ConfigMap `gateway-config` | 1 | Non-secret settings |
| ConfigMap `db-init` | 1 | The `db/*.sql` files |
| Secret `gateway-secrets` | 1 | API key + DB password. Created by hand, never committed |

---

## 3. Life of one request

🟢 **In simple words:** a tenant calls one number, gets connected to one of the gateway copies, which checks
the key in Postgres, checks the limits in Redis, calls Claude, and writes the bill.

```
 my Mac                          cluster (namespace llm-gateway)                      internet
 ──────                          ───────────────────────────────                      ────────
 Anthropic SDK / curl
   │  POST /v1/messages
   │  x-api-key: gw_…
   ▼
 localhost:8000
   │  kubectl port-forward svc/gateway 8000:80
   ▼
 Service gateway :80 ──▶ pod A :8000
                           │ 1. hash key, look up tenant ─────▶ postgres:5432
                           │ 2. take a ticket + hold quota ───▶ redis:6379
                           │ 3. write "before" usage row ─────▶ postgres:5432
                           │ 4. forward request ───────────────────────────────▶ OpenRouter
                           │                                                     (UPSTREAM_BASE_URL)
                           │ 5. stream reply back ◀──────────────────────────────  → Claude
                           │ 6. settle hold, write tokens + cost ▶ postgres / redis
   ◀───────────────────────┘
```

🔵 **Technically:**
- Pods find each other through **cluster DNS**. `postgres` resolves to the `postgres` Service's stable
  virtual IP, which forwards to whatever pod is currently behind it. That's why `DATABASE_URL` says
  `@postgres:5432` and not a pod IP. Pod IPs change every time a pod is replaced.
- `kubectl port-forward svc/gateway` is a debugging tunnel. It picks **one** pod and sticks to it, so it
  does *not* spread load across A and B. Real traffic from inside the cluster, or from a load balancer on
  OKE, does get spread by the Service.
- Egress (traffic leaving the cluster) to OpenRouter goes Pod → node → Colima → Mac → internet. On OKE the
  same hop would go through the NAT gateway in my private worker subnet.

---

## 4. How code gets into the cluster: now vs. after steps 3–5

### Now (steps 1–2, manual)

```
 gateway/*.py ──docker build──▶ image gateway:dev ──minikube image load──▶ node's image store
                                                                                │
 k8s/*.yaml ─────────────────────────kubectl apply -f k8s/──────────────────────▶ cluster
 .env ──────────kubectl create secret generic gateway-secrets …─────────────────▶ cluster
```

⚠️ **The `:dev` tag trap:** if I rebuild `gateway:dev` and load it again, **nothing restarts**. The
Deployment still says `image: gateway:dev`, which hasn't changed, so Kubernetes thinks there's nothing to
do. I'd need `kubectl rollout restart deploy/gateway`. This is exactly why CI will tag images with the
**git commit SHA**: a new commit means a new tag, which changes the Deployment, which triggers a rollout.

### Later (steps 3–5, GitOps)

🟢 **In simple words:** I stop touching the cluster. I change Git. A robot builds the image, another robot
notices Git changed and makes the cluster match. New versions first get a small slice of traffic, and only
go to everyone if they behave.

```
  git push / merge to main
          │
          ▼
  ┌─ GitHub Actions (step 4) ────────────────────────────┐
  │  build image ──▶ scan with Trivy ──▶ push to GHCR    │   fail the scan = nothing ships
  │                   (known CVEs?)      tag = git SHA   │
  │  update image tag in k8s/ (commit) ──────────────────┼──┐
  └──────────────────────────────────────────────────────┘  │
                                                            ▼
                                                   Git repo: k8s/*.yaml
                                                   (the single source of truth)
                                                            │  Argo CD polls Git
                                                            ▼
  ┌─ cluster ──────────────────────────────────────────────────────────────────┐
  │  Argo CD (step 3): "Git says X, cluster has Y" ──▶ apply the difference    │
  │                                                                            │
  │  Argo Rollouts (step 5), canary:                                           │
  │     new version  ─ 20% traffic ─▶ wait, check ─▶ 50% ─▶ 100%               │
  │     looks bad?   ─▶ abort, all traffic back to old version                 │
  └────────────────────────────────────────────────────────────────────────────┘
```

🔵 **Technically:** Argo CD is a **pull-based** deployer. It runs inside the cluster and pulls from Git, so
CI never needs cluster credentials. A leaked CI secret can push an image, but can't directly change
what runs. It also detects **drift**: if I `kubectl edit` something by hand, Argo CD shows it as
`OutOfSync` and can put it back.

---

## 5. Each piece, explained

### 5.1 Image (from `Dockerfile`)
🟢 A sealed lunchbox: code + exact dependencies + Python, the same everywhere it's opened.
🔵 Two stages. `uv sync --frozen` builds `.venv` in stage 1, and stage 2 copies only `.venv` + `gateway/` onto
`python:3.12-slim`. It runs as UID 10001, not root. `.dockerignore` keeps `.env` out of the build. 228 MB.

### 5.2 Deployment → ReplicaSet → Pod
🟢 The Deployment is a manager with a rule: "always 2 gateway copies of version X." If a copy dies, the
manager replaces it.
🔵 A Deployment owns a ReplicaSet (one per version), which owns the Pods. On a new version, it creates a new
ReplicaSet and scales old down / new up (rolling update). Argo Rollouts in step 5 replaces this for the
gateway with a smarter version that can pause and measure.

**Why 2 gateway replicas:** one pod can be replaced while the other keeps serving, and canary releases in
step 5 need more than one pod to split traffic across. The gateway keeps no state in memory (state lives
in Postgres and Redis), so extra copies are safe.

### 5.3 Service
🟢 A phone number that never changes, even when the people answering it do.
🔵 A stable virtual IP + DNS name (`gateway.llm-gateway.svc.cluster.local`, or just `gateway` from inside
the namespace) that forwards to every *ready* pod matching its label selector (`app: gateway`).
`ClusterIP` type means it's reachable only inside the cluster. On OKE, a `LoadBalancer` type or an
Ingress would expose it publicly.

### 5.4 ConfigMap vs Secret
🟢 ConfigMap = notice board (anyone can read it). Secret = locked drawer.
🔵 Both become env vars in the pod. The difference is permissions and handling, **not encryption**: a
Secret is only base64-encoded (a reversible text encoding, not a lock). Anyone who can
`kubectl get secret -o yaml` can read it. `DATABASE_URL` goes in the Secret because it contains the
password.

**Why the Secret is not in Git:** Git history is forever, and a pushed key is a leaked key. Downside:
Argo CD can't recreate it on a fresh cluster, so that's one manual `kubectl create secret` per cluster.
The proper fix later is an external secret store.

### 5.5 PersistentVolumeClaim (Postgres's disk)
🟢 A hard drive plugged into the Postgres pod. If the pod is replaced, the new one gets the same drive back.
🔵 The PVC asks the default StorageClass (minikube's `storage-provisioner`, a folder on the node) for 1 Gi.
On OKE the same PVC would be backed by an OCI Block Volume. **Recreate strategy:** the old Postgres pod is
stopped *before* the new one starts. A rolling update would briefly run two Postgres processes on the same
data folder, and that corrupts the database.

**Why Redis gets no disk:** Project 2 already made the counters rebuildable from Postgres (real tokens +
open holds). Losing Redis costs a short rebuild, not money. Fewer disks, fewer things to break.

### 5.6 Database setup (`db-init`)
🟢 The first time Postgres wakes up with an empty drive, it reads a folder of setup instructions.
🔵 The official Postgres image runs every `*.sql` in `/docker-entrypoint-initdb.d/` (alphabetical order,
hence the `001_…005_` prefixes) **only when the data folder is empty**. I mount `db/*.sql` there from the
`db-init` ConfigMap, which is generated from `db/`.
**Ceiling:** a future `006_*.sql` will NOT run on an existing database. When that happens, add a migration
Job. The ConfigMap is a generated copy of `db/`, so it must be regenerated when `db/` changes.

### 5.7 Probes (health checks)
🟢 Kubernetes regularly asks the gateway "are you OK?" If it stops answering, Kubernetes restarts it.
🔵 **Liveness** failing → the pod is restarted. **Readiness** failing → the pod is removed from the Service
(no traffic) but not restarted. Both hit the existing `GET /health`, which deliberately does **not** touch
the database. If it did, a 10-second Postgres blip would make every gateway pod fail liveness at once and
get restarted together, turning a small outage into a full one.
**Ceiling:** readiness says "ready" even when Postgres is down, so requests reach a pod that will return
errors. Upgrade path: a separate `/ready` that checks the DB pool, used only for readiness.

### 5.8 Resource requests and limits
🟢 Request = "reserve me this much room." Limit = "never let me take more than this."
🔵 The scheduler places pods using **requests**. Going over the memory **limit** gets the pod OOMKilled. Going
over the CPU limit just slows it down. Small values on purpose, because everything shares one 5 GB node:

| Pod | memory request / limit | CPU request |
|---|---|---|
| gateway (each) | 128Mi / 256Mi | 100m (0.1 core) |
| postgres | 256Mi / 512Mi | 100m |
| redis | 64Mi / 128Mi | 50m |

---

## 6. Decisions and tradeoffs (interview stories)

| Decision | What I chose | What I gave up | When the other option wins |
|---|---|---|---|
| Local cluster | minikube (already installed) | Real cloud networking, LBs, Workload Identity | When I have budget, or need to test OCI-specific behaviour |
| Manifest format | Plain YAML in `k8s/` | No templating, some copy-paste later | Several environments (local + OKE) → Kustomize overlays |
| Databases | In-cluster Postgres + Redis | Backups, patching, HA | Any real data → managed DB (OCI Database / Autonomous) |
| Postgres controller | Deployment + PVC + Recreate | StatefulSet's stable identity per replica | Multiple DB replicas (primary + standby) |
| Redis storage | None | Counters reset on restart (rebuilt from Postgres) | If rebuild were slow or Redis held data found nowhere else |
| Secrets | Manual `kubectl create secret` from `.env` | Full GitOps, since one object lives outside Git | Team / multiple clusters → Sealed Secrets or External Secrets + OCI Vault |
| Schema setup | Postgres init folder | Only runs on an empty DB | The first new migration → a migration Job |
| Image delivery | `minikube image load` now, GHCR in step 4 | Registry practice until step 4 | Any real cluster: they can't see my laptop's images |
| Probes | Liveness + readiness on `/health` | Readiness doesn't see DB outages | Add `/ready` once traffic matters |

---

## 7. What could go wrong (and how I'd spot it)

| Symptom (`kubectl get pods -n llm-gateway`) | Likely cause | First thing to check |
|---|---|---|
| `Pending` | Not enough CPU/memory left on the node | `kubectl describe pod …` → Events: `Insufficient memory` |
| `ErrImagePull` / `ImagePullBackOff` | Image not loaded into minikube, or wrong tag | `minikube image ls \| grep gateway` |
| `CrashLoopBackOff` (gateway) | Missing env var (`KeyError: 'UPSTREAM_API_KEY'`) or DB unreachable at startup | `kubectl logs deploy/gateway` |
| `CreateContainerConfigError` | Secret `gateway-secrets` doesn't exist yet | `kubectl get secret -n llm-gateway` |
| `OOMKilled` in `describe` | Memory limit too low | Raise the limit, or find the leak |
| Tables missing | `db-init` didn't run because the volume already had data | `kubectl exec deploy/postgres -- psql -U gateway -c '\dt'` |
| Code change not live | Same `:dev` tag, so no rollout | `kubectl rollout restart deploy/gateway` |
| 401 on every call | New database = no tenants yet | Create a tenant inside the cluster (section 10) |

---

## 8. Startup order (and why it doesn't matter much)

Kubernetes starts everything at once, with no "wait for Postgres" step. The gateway might start before
Postgres is ready, fail to open its DB pool in `lifespan`, and crash. Kubernetes then restarts it with a
growing delay (`CrashLoopBackOff`), and within a few tries Postgres is up and the gateway starts fine.
That's normal Kubernetes behaviour: **crash and retry instead of carefully ordering the startup**. The only
cost is a few noisy restarts in the first minute.

---

## 9. Later: minikube → OKE (what changes)

```
                    minikube (now, $0)              OKE (when funded)
                    ──────────────────              ─────────────────
 where it runs      container in Colima             ARM/AMD VMs in private subnet
 cluster created by minikube start                  terraform apply (infra/cluster)
 image comes from   minikube image load → GHCR      GHCR (or OCI Registry)
 public access      kubectl port-forward            Service type LoadBalancer → OCI LB
                                                    in the public LB subnet
 Postgres disk      folder on the node              OCI Block Volume
 cloud permissions  none needed                     Workload Identity (no static keys)
 k8s/*.yaml         ─────────────── mostly the SAME files ───────────────
 Argo CD            points at minikube              points at OKE (same Git repo)
```

That bottom row is the payoff of GitOps: the deploy definitions don't care which cluster runs them.

---

## 10. Command cheat sheet

```bash
# Boxes
colima start --cpu 4 --memory 6                  # the Linux VM (Docker engine)
minikube start --driver=docker --cpus=4 --memory=5g
minikube stop                                    # free the RAM when done; the cluster is kept

# Image: CI builds and pushes it on every merge to main (step 4). By hand only
# to try something before pushing (then `kubectl rollout restart deploy/gateway`):
docker build -t gateway:dev .
minikube image load gateway:dev

# GHCR pull secret (once per cluster; never committed). The token is a GitHub
# classic PAT with ONLY read:packages: it can pull images, nothing else.
kubectl create secret docker-registry ghcr-pull -n llm-gateway \
  --docker-server=ghcr.io --docker-username=musishere --docker-password=<PAT>

# Secret (once per cluster, from .env; never committed)
# New random DB password each fresh cluster; the API key is copied from .env.
kubectl apply -f k8s/00-namespace.yaml
PW=$(openssl rand -hex 16); KEY=$(grep '^UPSTREAM_API_KEY=' .env | cut -d= -f2-)
kubectl create secret generic gateway-secrets -n llm-gateway \
  --from-literal=POSTGRES_PASSWORD="$PW" \
  --from-literal=DATABASE_URL="postgresql://gateway:$PW@postgres:5432/gateway" \
  --from-literal=UPSTREAM_API_KEY="$KEY"

# Deploy + look
kubectl apply -f k8s/
kubectl get pods -n llm-gateway -w
kubectl logs -n llm-gateway deploy/gateway -f
kubectl describe pod -n llm-gateway <pod>        # the "Events" section explains most failures

# Use it
kubectl port-forward -n llm-gateway svc/gateway 8000:80
kubectl exec -n llm-gateway deploy/gateway -- python -m gateway.create_tenant acme

# Clean slate (deletes the Postgres data too)
kubectl delete namespace llm-gateway
```

---

## 11. Check yourself

**Q1.** I rebuild the image with a bug fix, run `minikube image load gateway:dev`, and the bug is still there. Why?
<details><summary>Answer</summary>
The Deployment's spec didn't change (still `gateway:dev`), so Kubernetes sees nothing to roll out and the
old pods keep running the old image. Fix now: `kubectl rollout restart`. Fix properly: unique tags per
commit (step 4). See section 4.
</details>

**Q2.** Postgres goes down for 20 seconds. What happens to the 2 gateway pods, and why is that the behaviour we want?
<details><summary>Answer</summary>
Nothing restarts, because `/health` doesn't touch the database, so liveness keeps passing. Requests during
those 20 seconds fail (auth can't look up tenants, so it fails closed), and then everything recovers on its
own. If liveness checked the DB, both pods would be restarted together, and after Postgres came back
they'd still be in restart back-off, so the outage would last longer. See 5.7.
</details>

**Q3.** Why would a rolling update be dangerous for the Postgres Deployment, but fine for the gateway?
<details><summary>Answer</summary>
A rolling update starts the new pod before stopping the old one. For the gateway that's the goal (no
downtime, no shared state). For Postgres it means two database processes writing to the same data folder
on the same volume, which corrupts it. `Recreate` stops the old one first and accepts a few seconds of
downtime. See 5.5.
</details>

**Q4.** The Redis pod is deleted. Does anyone get free tokens, or get blocked unfairly?
<details><summary>Answer</summary>
Neither. For the few seconds before the replacement Redis pod is up, calls get 503 (Redis down → fail
closed, my Project 2 choice). After that, Redis is reachable but empty, and the gateway rebuilds each
counter from Postgres (real usage + open holds). No free tokens, and no unfair blocks. See 5.5.
</details>

**Q5.** Why does Argo CD pulling from Git make things safer than CI pushing with `kubectl apply`?
<details><summary>Answer</summary>
With pull, the cluster credentials never leave the cluster. CI only needs permission to push images and
commit to Git. With push, CI holds admin credentials for the cluster, and a leaked CI secret means
someone else controls production. Pull also catches drift. See section 4.
</details>

---

## 12. Key terms

| Simple words | Technical term | One line |
|---|---|---|
| Sealed lunchbox | Image | Code + dependencies + runtime, built once, runs anywhere |
| A running lunchbox | Container / Pod | A pod is 1+ containers sharing a network address |
| The manager | Deployment | Keeps N pods of a version running, and rolls out new versions |
| The phone number | Service | Stable DNS name + virtual IP in front of changing pods |
| Notice board / locked drawer | ConfigMap / Secret | Settings injected as env vars. Secret ≠ encrypted |
| Plugged-in hard drive | PersistentVolumeClaim | A request for storage that outlives the pod |
| "Are you OK?" | Liveness / readiness probe | Restart it / stop sending it traffic |
| Reserve / ceiling | Resource request / limit | What scheduling uses / what gets you OOMKilled |
| Robot that makes the cluster match Git | Argo CD (GitOps) | Pull-based continuous delivery with drift detection |
| Taste-test before serving everyone | Canary release | Send a small % of traffic to the new version, then expand or abort |
| Folder for related things | Namespace | A name scope + a unit for cleanup and permissions |

---
---

# Part 2: Under the hood

## 13. The one idea behind everything: "desired state" + a loop that fixes differences

🟢 **In simple words:** a thermostat. You don't tell the heater "turn on for 20 minutes." You say
"I want 22°C." The thermostat keeps checking the room and switches the heater on or off until the room
matches. If someone opens a window, it notices and fixes it again. It never "finishes."

🔵 **Technically:** this is a **control loop** (also called **reconciliation**): *observe actual state →
compare with desired state → act on the difference → repeat forever*. Kubernetes is dozens of these loops.
Argo CD is one more loop stacked on top, with Git as its "thermostat setting."

```
 WHAT I WANT                                      WHAT ACTUALLY RUNS
 ───────────                                      ──────────────────
 Git: k8s/*.yaml ──(Argo CD loop)──▶ API server/etcd ──(Kubernetes loops)──▶ pods & containers
   "2 gateways"     compares Git      "2 gateways"      Deployment ctrl,      2 real processes
                    vs cluster,       (desired state    ReplicaSet ctrl,      on the node
                    applies diff       stored here)     scheduler, kubelet

   loop 1: Git ↔ cluster (Argo CD)   loop 2: object ↔ pods (controllers)   loop 3: pod ↔ container (kubelet)
```

Once this idea clicks, everything else is a detail:
- **The 4 restarts**: kubelet's loop kept trying to make "a running gateway container" true.
- **The self-heal test**: I changed the middle box by hand, and Argo CD's loop made it match Git again.
- **Scaling**: I never "start a pod." I change a number and the loops make it true.

**Why this matters (interview line):** imperative = "do these steps" (breaks if one step fails halfway).
Declarative = "this is the end state" (the loops retry until it's true, and repair drift later).

---

## 14. Layer 1: the image, under the hood

### What `docker build -t gateway:dev .` really did

```
 1. Build context      the "." folder is sent to the Docker engine (inside Colima)
                       MINUS everything in .dockerignore (.env, .venv, .git, infra …)
                                          │
 2. Stage "build"      python:3.12-slim + uv binary
                       COPY pyproject.toml uv.lock   ← only the lock files
                       RUN uv sync --frozen          ← creates /app/.venv
                                          │
 3. Stage 2 (final)    fresh python:3.12-slim
                       useradd app (uid 10001)
                       COPY --from=build /app/.venv  ← only the result crosses over
                       COPY gateway/
                       USER 10001, CMD uvicorn …
                                          │
 4. Tag                that final stack of layers gets the name gateway:dev
```

An image is a **stack of read-only layers**. Each Dockerfile instruction adds one. My real layers
(`docker history gateway:dev`):

```
   size     instruction
   0B       CMD ["uvicorn" …]        ← metadata only, no files
   0B       USER 10001
   131kB    COPY gateway/            ← my code: tiny, and changes often
   39.3MB   COPY .venv               ← dependencies: big, change rarely
   41kB     useradd app
   ~150MB   python:3.12-slim base    ← shared with any other image on the same base
```

**Why the order matters:** Docker reuses (caches) a layer if its instruction and inputs didn't change,
**but only until the first layer that did change**. Everything after that is rebuilt. Lock files come
before code, so editing `gateway/main.py` rebuilds only the 131 kB layer, not the 39 MB one. If I'd written
`COPY . .` first, every code edit would reinstall every dependency.

**Why `.dockerignore` is a security control, not tidiness:** the build context is sent in full *before*
the Dockerfile runs. Without it, `.env` reaches the engine, and one careless `COPY . .` bakes the API key
into a layer. Layers are permanent: a later `RUN rm .env` adds a *new* layer that hides the file, while the
old layer still contains it. Anyone who pulls the image can extract it.

### Why `minikube image load` was needed: there are two Docker engines

```
 Colima VM
 ├── Docker engine #1 ← `docker build` put gateway:dev HERE
 └── container "minikube" (the Kubernetes node)
     └── Docker engine #2 ← kubelet starts pods from HERE; it can't see engine #1's images
```

`minikube image load` copies the image from #1 into #2. On a real cluster there's no shared laptop, so
nodes pull from a **registry** (GHCR in step 4). That's why `imagePullPolicy: Never` is only a
local-development setting.

---

## 15. Layer 2: the cluster, under the hood

### The control plane (the parts that run the loops)

My cluster's real system pods (`kubectl get pods -n kube-system`):

| Pod | Simple words | What it does |
|---|---|---|
| `kube-apiserver-minikube` | The front desk | The **only** way in. `kubectl`, controllers, Argo CD all talk to it. It checks permissions and validates objects |
| `etcd-minikube` | The filing cabinet | A key-value database holding every object (the desired state). Lose etcd = lose the cluster's memory |
| `kube-controller-manager-minikube` | The managers | Runs the built-in loops: Deployment, ReplicaSet, endpoints, PVC binding … |
| `kube-scheduler-minikube` | The seating planner | Picks a node for each new pod, based on its **requests** vs free room |
| `kube-proxy-…` | The switchboard | Programs the node's network rules so Service IPs reach pod IPs |
| `coredns-…` | The phone book | Answers "what's the IP of `postgres`?" |
| `storage-provisioner` | The hardware store | Makes a disk (a folder, on minikube) when a PVC asks for one |
| *(kubelet, not a pod)* | The hands | An agent on each node that actually starts and stops containers and runs probes |

### What happened when `kubectl apply -f k8s/gateway.yaml` ran: step by step

```
 kubectl ──1──▶ API server ──2──▶ etcd: Deployment "gateway" (replicas: 2) stored
                    │
                    │ 3. Deployment controller (watching) sees a new Deployment
                    │    → creates ReplicaSet gateway-6b778585c9  (hash of the pod template)
                    │ 4. ReplicaSet controller: "want 2 pods, have 0" → creates 2 Pod objects
                    │    (no node yet, status Pending)
                    │ 5. Scheduler: "pod needs 100m CPU + 128Mi, minikube node has room"
                    │    → writes nodeName: minikube on each pod
                    ▼
               kubelet on node minikube (watching for pods assigned to it)
                 6. image gateway:dev present? (pullPolicy Never → must be, else ErrImagePull)
                 7. reads ConfigMap gateway-config + Secret gateway-secrets → env vars
                 8. starts the container as uid 10001 → uvicorn → lifespan() → connect to postgres
                 9. runs readiness probe GET /health every 5s → passes → pod marked Ready
                                    │
 10. Endpoints controller: pod is Ready and has label app=gateway
     → adds its IP to the EndpointSlice of Service "gateway" → it starts receiving traffic
```

**The real ownership chain** (each object has an `ownerReference` to its parent):

```
 Deployment/gateway
   └── ReplicaSet/gateway-6b778585c9          (6b778585c9 = hash of the pod template)
         ├── Pod/gateway-6b778585c9-vqpls     ip 10.244.0.5
         └── Pod/gateway-6b778585c9-xddqn     ip 10.244.0.14   ← the one self-heal created
```

Owners matter for cleanup: delete the Deployment and Kubernetes' garbage collector deletes the ReplicaSet,
then the pods. A new image or env change produces a **new pod-template hash**, so a new ReplicaSet appears,
and the rollout is the Deployment shifting pods from the old ReplicaSet to the new one.

**Labels are the glue.** Nothing is linked by name. The Deployment finds its pods by `app: gateway`, and so
does the Service. A typo in a label = a Service with no endpoints = "connection refused" with every pod
looking healthy.

---

## 16. Networking under the hood: how `postgres` becomes a working connection

### Step 1: name → IP (CoreDNS)

Inside every pod, `/etc/resolv.conf` (real, from my gateway pod):
```
nameserver 10.96.0.10                                         ← CoreDNS's Service IP
search llm-gateway.svc.cluster.local svc.cluster.local cluster.local
```
The gateway asks for `postgres`. The `search` line makes the resolver try
`postgres.llm-gateway.svc.cluster.local` first, and CoreDNS answers with the Service's IP:
```
postgres -> 10.110.99.166        redis -> 10.98.31.225        (real answers from inside the pod)
```
That's why a short name works inside the same namespace, and why another namespace would need
`postgres.llm-gateway`.

### Step 2: Service IP → pod IP (kube-proxy)

🟢 The Service IP is like a company's main phone number. No desk actually has that number; the
switchboard forwards each call to someone who's in today.

🔵 **Nothing listens on 10.110.99.166.** It's a **virtual IP**. kube-proxy writes network rules (iptables)
on the node: "packets to 10.110.99.166:5432 → rewrite the destination to one of the *ready* pod IPs in
the EndpointSlice." The gateway Service's real EndpointSlice right now:
```
10.244.0.5  ready=true
10.244.0.14 ready=true
```

### This explains the 4 restarts exactly

The crash log said `Connect call failed ('10.110.99.166', 5432)`, which is the **postgres Service IP**. At
that moment the Postgres pod existed but wasn't *ready* (its image was still downloading), so the
EndpointSlice was **empty**. For a Service with no endpoints, kube-proxy **rejects** the connection
immediately, hence "connection refused" rather than a timeout. Then:

```
 t≈0s    gateway starts → lifespan → connect postgres → refused → process exits → container dies
 t≈10s   kubelet restarts it (back-off 10s)  → refused again
 t≈30s   restart (back-off 20s)              → refused again
 t≈70s   restart (back-off 40s)              → refused again
 t≈…     postgres pulled, pg_isready passes → added to EndpointSlice
         restart #4 → connect OK → /health passes → Ready → Running, restarts stay at 4
```
The back-off doubles each time (10s, 20s, 40s … capped at 5 minutes). That's what `CrashLoopBackOff`
means: *crashing, and waiting longer between each retry*. It's a status, not an error type.

### Traffic leaving the cluster (to OpenRouter)
Pod `10.244.x.x` → node → Colima VM → Mac → internet. Each hop rewrites the source address (NAT), so
OpenRouter sees my home IP. On OKE the pods sit in a **private** subnet and leave through the NAT gateway
built in `infra/cluster/network.tf`: out only, nothing can call in.

---

## 17. Config and Secrets under the hood

**Env vars are copied in when the container starts, and then frozen.**
If Argo CD updates `gateway-config` (say a new `UPSTREAM_BASE_URL`), the **running pods keep the old value**.
The ConfigMap changed, but the pod template didn't, so there's no new ReplicaSet and no rollout.
Fix now: `kubectl rollout restart deploy/gateway`. Proper fix later: Kustomize's `configMapGenerator`
adds a content hash to the ConfigMap's name, so a change produces a new name, which changes the pod
template and triggers a rollout automatically.

**A Secret is not encrypted, only encoded.**
```bash
kubectl get secret gateway-secrets -n llm-gateway -o jsonpath='{.data.POSTGRES_PASSWORD}'
# → something like  MmJhYmMy…   (base64: anyone can decode it with `base64 -d`)
```
The protection is **who can read it** (RBAC, Kubernetes' permission system), not the encoding. On minikube,
etcd stores it unencrypted on disk. Managed clusters like OKE encrypt etcd's disk for you.

**Every secret in the system today:**

| Secret | Where it lives | Who can use it | If it leaks |
|---|---|---|---|
| OpenRouter API key | `.env` on Mac → Secret `gateway-secrets` | gateway pods | Someone spends my OpenRouter credit. Rotate at OpenRouter |
| Postgres password | Secret `gateway-secrets` only (random, never on disk) | postgres + gateway | Only reachable inside the cluster, so low risk |
| Deploy key (private) | `~/.ssh/argocd_ai_platform` + Secret `repo-ai-platform` | Argo CD repo-server | Read my code (read-only). Delete the key in GitHub |
| Tenant keys | Only printed once. Postgres stores SHA-256 hashes | tenants | A DB leak exposes no working keys (Project 1 design) |
| Argo CD admin password | Secret `argocd-initial-admin-secret` | me | Full control of what's deployed. Change it / delete that Secret on a real cluster |

---

## 18. Storage under the hood: what "the disk survives" really means

```
 PVC postgres-data (1Gi, "I need a disk")
   │  storage-provisioner sees an unbound claim
   ▼
 PV pvc-… (the actual disk) = folder /tmp/hostpath-provisioner/… on the minikube node
   │  bound 1:1 to the claim
   ▼
 mounted into the postgres pod at /var/lib/postgresql/data  (data in …/pgdata)
```

| I do this | Data survives? | Why |
|---|---|---|
| Delete the postgres **pod** | ✅ | The Deployment makes a new pod, which mounts the same PVC |
| Update Postgres (Recreate) | ✅ | Same PVC, old pod stops first |
| Delete the **namespace** | ❌ | PVC deleted → PV deleted (reclaim policy `Delete`) |
| `minikube delete` | ❌ | The whole node (and its folders) is gone |
| Remove `postgres.yaml` from Git | ❌ | Argo CD **prune** deletes the PVC |

On OKE the same PVC would create an **OCI Block Volume**, a real network disk that could even move to
another node if Postgres's pod got rescheduled.

---

## 19. Argo CD under the hood

### Its parts (real pods in namespace `argocd`)

| Pod | Simple words | Job |
|---|---|---|
| `argocd-repo-server` | The reader | Clones Git with the deploy key and turns `k8s/` into a list of objects (would also render Helm/Kustomize) |
| `argocd-application-controller-0` | The thermostat | Compares the list from Git with the live cluster, applies differences, judges health |
| `argocd-server` | The dashboard | Web UI + API (what `port-forward 8080:443` opens) |
| `argocd-redis` | Its notepad | A cache for Argo CD itself. **Not** my gateway's Redis |
| `argocd-dex-server` | SSO door | Login via GitHub/Google etc. (unused, I log in as admin) |
| `argocd-applicationset-controller` | Card printer | Generates many Applications from one template (unused) |
| `argocd-notifications-controller` | Messenger | Slack/email on sync events (unused) |

### One loop, step by step

```
                    ┌───────────────────── about every 3 min (or on a webhook, or "Refresh") ─┐
                    ▼                                                                          │
 1. repo-server: git fetch main over SSH (deploy key)                                         │
    checks github.com's host key against argocd-ssh-known-hosts-cm                            │
    (so a fake "github.com" can't feed it manifests)                                          │
 2. renders k8s/ → DESIRED objects        (cached per commit SHA, e.g. 8cbb282)               │
                    │                                                                          │
 3. controller: LIVE objects ◀── it keeps a live watch on the cluster, updated instantly      │
 4. diff DESIRED vs LIVE → sync status: Synced / OutOfSync                                    │
 5. OutOfSync + automated → apply the difference (in a safe order, see below)                 │
    selfHeal → also re-apply when the LIVE side drifted                                       │
    prune    → delete LIVE objects it owns that are no longer in DESIRED                      │
 6. health check of every object → Healthy / Progressing / Degraded / Missing ────────────────┘
```

**Why self-heal took 4 seconds but a git push can take up to ~3 minutes:** the two sides are watched
differently. The cluster side is a **live watch**, so the controller heard about my `kubectl scale` right away.
The Git side is **polled** every ~3 minutes, because Argo CD has no way to know a commit happened until
it asks. Teams add a GitHub **webhook** ("GitHub calls Argo CD when you push") to make it instant, but
that needs Argo CD to be reachable *from* the internet, which a laptop cluster (or a private OKE cluster)
isn't. Polling is the price of not exposing anything.

### Two separate questions: "sync" vs "health"

| | Question | Values |
|---|---|---|
| **Sync status** | Does the cluster match Git? | `Synced`, `OutOfSync` |
| **Health status** | Is what's running actually working? | `Healthy`, `Progressing`, `Degraded`, `Missing` |

They're independent. **Synced + Degraded** = "I applied exactly what Git says, and it's broken," which is
what a bad release looks like. It's also why Argo CD alone doesn't protect you from bad code: it faithfully
deploys it. That's the job of the canary in step 5.

For a Deployment, "Healthy" means the rollout finished and the wanted replicas are available (readiness
probes passing). ConfigMaps and Services have no health, which is why they showed a blank.

### How it "adopted" what I'd created by hand

Argo CD marks every object it manages with an annotation (real, from my gateway Deployment):
```
argocd.argoproj.io/tracking-id: gateway:apps/Deployment:llm-gateway/gateway
                                └app┘ └── kind ──────┘ └namespace/name┘
```
On the first sync, my hand-made objects had the same kind, namespace and name as Git, so Argo CD applied
Git's version on top and stamped the annotation. No duplicates, no restarts. **Prune only deletes objects
that carry this annotation**, which is why `gateway-secrets` (created by hand, never stamped) is safe.

### Order of applying

Argo CD sorts objects by kind before applying: Namespace → Secrets/ConfigMaps → PVCs → Services →
Deployments … So the namespace exists before anything inside it, whatever the filenames are. The `00-` prefix
is only for plain `kubectl apply -f k8s/`, which goes alphabetically.

### Its memory of what it deployed
```
history: id 0 → revision 8cbb282 deployed 2026-09-29T10:41:10Z
```
Every sync records the Git commit it deployed. A rollback in GitOps is normally `git revert` (so Git stays
the truth). The history tells you *which* commit was live when something broke.

---

## 20. The full picture: one change, end to end (today vs. after step 4)

**Today, if I change `replicas: 2 → 3` in `k8s/gateway.yaml` and push:**
```
 git push ─▶ GitHub main (new SHA)
             ... up to ~3 min: Argo CD's next poll fetches it ...
 repo-server renders → controller diffs: Deployment.spec.replicas 2 ≠ 3 → OutOfSync
 → applies → Deployment controller → ReplicaSet wants 3 → +1 Pod → scheduler → kubelet
 → readiness passes → EndpointSlice gets a 3rd IP → Synced + Healthy, history id 1 = new SHA
```

**If I change gateway *code* today:** Git alone does nothing useful. The image `gateway:dev` is built and
loaded by hand, and the Deployment's text never changes. **This is the gap step 4 closes:** CI builds an
image tagged with the commit SHA and commits that tag into `k8s/gateway.yaml`. Then a code change *is*
a manifest change, and Argo CD rolls it out like the replicas example.

---

## 21. What's still manual, and rebuilding from zero

Everything in Git rebuilds itself. These don't, on purpose (they're either secrets or the thing that
starts the robot):

```bash
# 1. The boxes
colima start --cpu 4 --memory 6
minikube start --driver=docker --cpus=4 --memory=5g
# 2. The image: nothing to do, CI pushed it to GHCR. Pods pull it with ghcr-pull (step 4).
# 3. Argo CD itself (pinned version)
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml
# 4. Secrets (never in Git): the repo key for Argo CD, the app secrets (section 10)
kubectl create secret generic repo-ai-platform -n argocd --from-literal=type=git \
  --from-literal=url=git@github.com:musishere/AI-platform-engineering.git \
  --from-file=sshPrivateKey=$HOME/.ssh/argocd_ai_platform
kubectl label secret repo-ai-platform -n argocd argocd.argoproj.io/secret-type=repository
#    + gateway-secrets and ghcr-pull (section 10; the namespace is created by step 5, or apply 00-namespace.yaml first)
# 5. The bootstrap: from here on, Git drives
kubectl apply -f argocd/gateway.yaml
```

`--server-side` in step 3: Argo CD's own object definitions (CRDs, "custom resource definitions", which
teach Kubernetes new kinds like `Application`) are too big for normal `kubectl apply`. It saves a copy of
each object in an annotation limited to 256 KB. Server-side apply keeps that bookkeeping in the API server
instead.

On OKE, steps 1 (and eventually 3–5) become Terraform. That's the "Terraform builds the building,
GitOps arranges the furniture" split.

---

## 22. Check yourself (Part 2)

**Q6.** I push a new `UPSTREAM_BASE_URL` in `k8s/config.yaml`. Argo CD shows Synced + Healthy. Are the gateways using the new URL?
<details><summary>Answer</summary>
No. Env vars are read once at container start. The ConfigMap changed, but the Deployment's pod template
didn't, so there was no rollout and the pods keep the old value. `kubectl rollout restart`, or a hashed
ConfigMap name via Kustomize. See 17.
</details>

**Q7.** I make a typo: the gateway Service's selector says `app: gatway`. All pods are Running and Ready. What does a caller see, and why?
<details><summary>Answer</summary>
Connection refused. The selector matches no pods, the EndpointSlice is empty, and kube-proxy rejects
connections to a Service with no endpoints. Same symptom as the 4 startup restarts, but permanent.
Check with `kubectl get endpointslice`. See 15–16.
</details>

**Q8.** Argo CD says **Synced + Degraded**. Whose fault is it, Argo CD's or the release's, and would rolling back with `kubectl rollout undo` stick?
<details><summary>Answer</summary>
The release's: Argo CD applied exactly what Git says, and what Git says doesn't work. `kubectl rollout undo`
won't stick, because selfHeal sees the cluster no longer matches Git and re-applies the broken version.
The GitOps rollback is `git revert` + push. See 19.
</details>

**Q9.** Why did deleting a gateway pod by hand (or scaling it) cause no downtime, but deleting the Postgres pod would?
<details><summary>Answer</summary>
The gateway has 2 ready replicas behind one Service, and the other keeps serving while the loop replaces
the missing one. Postgres has 1 replica with `Recreate`, so until the new pod is ready the Service has no
endpoints and the gateway's DB calls fail. See 5.5, 15, 16.
</details>
