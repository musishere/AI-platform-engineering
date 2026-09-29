# Deployment: how the gateway runs on Kubernetes

> **Status (2026-09-29):** Steps 1–2 done: the gateway runs on minikube from `k8s/` and a real call was metered end to end. Step 3 (Argo CD) is next.
> The OKE deploy is paused until funded, so everything below runs on **minikube on my Mac, for $0**.
> Moving to OKE later changes the "outside" of this picture, not the inside (see section 9).

**One-sentence version:** my gateway code is packed into an **image**, Kubernetes runs **2 copies** of it
next to a **Postgres** and a **Redis**, and a **Service** gives them one stable address. Later, Git becomes
the single source of truth: CI builds the image and Argo CD makes the cluster match what Git says.

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

# Image
docker build -t gateway:dev .
minikube image load gateway:dev

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
