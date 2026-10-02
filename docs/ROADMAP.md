# AI Platform Engineering Roadmap

Backend engineer (~3.5 years, Lahore) working as an AI agent engineer at ByteForge, aiming for a **Senior AI Platform Engineer** role that sponsors a visa. The plan is 9 months at 12–15 hours/week alongside a full-time job. It is built on 8 projects, and each one extends the previous: a Claude API gateway grows quotas, real infrastructure, monitoring, a self-hosted model, cost-aware routing, shared guardrail and eval services, and finally a fine-tuned model served behind it. Theory gets read only when a project needs it.

## Progress

- [ ] 1. LLM gateway
- [ ] 2. Quotas and rate limits
- [ ] 3. Deploy it properly
- [ ] 4. Monitoring
- [ ] 5. Self-hosted model
- [ ] 6. Gateway v2
- [ ] 7. Guardrails and evals as shared services
- [ ] 8. LoRA fine-tune, served behind the gateway

## Timeline

| Month | Project | Outcome |
|-------|---------|---------|
| 1 | 1. LLM gateway | FastAPI proxy to Claude with per-tenant keys and usage metering in Postgres |
| 1–2 | 2. Quotas and rate limits | Redis token bucket and monthly token quotas enforced per tenant |
| 2 | 3. Deploy it properly | Gateway on OKE (Oracle Cloud) via Terraform and Argo CD, with a CI/CD canary pipeline |
| 3 | 4. Monitoring | Metrics, traces, SLOs and burn-rate alerts for the gateway |
| 3–4 | 5. Self-hosted model | vLLM on a rented GPU, benchmarked across concurrency and quantization |
| 4–5 | 6. Gateway v2 | Cost/task routing between Claude and vLLM with fallback, plus a published benchmark post |
| 6–7 | 7. Guardrails and evals | PII redaction, injection screening and evals as versioned shared services, plus one merged PR to vLLM or LiteLLM |
| 8–9 | 8. LoRA fine-tune | Fine-tuned small model tracked in MLflow, eval-gated, served as a vLLM multi-LoRA adapter |

---

## Project 1: LLM gateway (Month 1)

**Goal:** A FastAPI proxy in front of the Claude API that authenticates tenants and meters every call.

**What to build**
- [x] FastAPI service that forwards requests to the Claude API
- [x] Per-tenant API keys, stored hashed
- [x] Usage metering into Postgres: tokens, cost, latency, status
- [x] Stretch: streaming with correct metering, including client disconnects

**What I'll learn**
- Proxying HTTP requests asynchronously in FastAPI
- Storing and checking hashed API keys
- Postgres schema design for tenants and usage events
- Computing token cost and measuring latency per request
- (Stretch) Streaming responses, and metering when the client disconnects mid-stream

**Done when**
- [x] A request with a valid tenant key returns Claude's response, and an unknown key returns 401
- [x] Every call writes one usage row with tokens, cost, latency and status
- [x] No plaintext API keys exist in the database

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 2: Quotas and rate limits (Months 1–2)

**Goal:** Enforce per-tenant rate limits and monthly token quotas in the gateway using Redis.

**What to build**
- [x] Token bucket rate limiter in Redis
- [x] Monthly token quota per tenant
- [x] Reject requests that exceed the rate limit or the quota

**What I'll learn**
- How the token bucket algorithm works
- Atomic counters and expiry in Redis
- Keeping quota counts correct under concurrent requests

**Done when**
- [x] A burst above a tenant's limit gets rejected while normal traffic passes
- [x] A tenant that uses up its monthly token quota is blocked until the quota resets
- [x] Concurrent requests do not let a tenant go over its quota

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 3: Deploy it properly (Month 2)

**Goal:** Run the gateway on infrastructure I designed myself, not just deployed onto.

**Cloud:** Oracle Cloud (OCI), home region Mumbai (`ap-mumbai-1`), in place of AWS. Always Free resources only, except Workload Identity, which needs an enhanced cluster and must be done within the $300 / 30-day trial (ends ~2026-10-28). Build, test, destroy each session.

**Status (2026-09-29):** OKE deploy deferred until funded (free ARM workers are OUT_OF_HOST_CAPACITY in Mumbai, and I'm not paying). Meanwhile Argo CD, CI/CD and canary are built on a local minikube cluster ($0); switching to OKE later means pointing Argo CD at the new cluster.

**Status (2026-09-30):** CI/CD done on minikube: build → Trivy scan → push to GHCR → tag commit → Argo CD → Argo Rollouts canary (25% → smoke test → 50% → 100%). Remaining Project 3 items (OKE cluster, Workload Identity) are blocked on OCI capacity/funding.

**What to build**
- [x] Terraform: remote state (Object Storage), done first so no paid resource is ever tracked only on my laptop
- [x] Terraform: VCN (OCI's VPC)
- [ ] Terraform: OKE cluster (OCI's EKS)
- [ ] Terraform: OKE Workload Identity for pod-level OCI permissions (OCI's IRSA)
- [x] Argo CD deploying the gateway
- [x] CI/CD pipeline: build
- [x] CI/CD pipeline: scan
- [x] CI/CD pipeline: push
- [x] CI/CD pipeline: canary release

**What I'll learn**
- VCN layout (subnets, routing) for an OKE cluster
- Workload Identity for pods (OCI's IRSA)
- Terraform remote state and how to structure modules
- GitOps with Argo CD
- Canary rollouts and when to roll back

**Done when**
- [ ] `terraform apply` from scratch brings up the VCN and OKE cluster, with state stored remotely
- [x] A merge to main builds, scans and pushes an image, and Argo CD rolls it out as a canary
- [ ] The gateway pod reaches OCI through Workload Identity, with no static credentials

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 4: Monitoring (Month 3)

**Goal:** Know when the gateway is unhealthy before tenants notice.

**What to build**
- [x] Prometheus metrics from the gateway
- [x] Grafana dashboards
- [ ] OpenTelemetry traces across the request path
- [ ] SLOs for the gateway
- [ ] Burn-rate alerts on those SLOs

**What I'll learn**
- Which metrics matter for an LLM proxy (latency, errors, tokens, cost)
- Distributed tracing with OpenTelemetry
- Defining SLOs and error budgets
- Multi-window burn-rate alerting

**Done when**
- [x] A Grafana dashboard shows request rate, error rate, latency and token usage per tenant
- [ ] One request can be followed end to end in a trace
- [ ] Injecting errors fires a burn-rate alert

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 5: Self-hosted model (Months 3–4)

**Goal:** Serve an open model with vLLM and measure how it performs.

**Constraint:** GPU budget cap of $50–100/month, short rental windows only.

**What to build**
- [ ] vLLM running on a rented GPU
- [ ] Benchmark: time to first token
- [ ] Benchmark: per-token latency
- [ ] Benchmark: throughput vs. concurrency
- [ ] Compare quantization levels

**What I'll learn**
- How vLLM serves models (batching, KV cache)
- The latency vs. throughput tradeoff as concurrency rises
- How quantization affects speed, memory and quality
- Running benchmarks that can be repeated within short GPU rental windows

**Done when**
- [ ] A benchmark script produces TTFT, per-token latency and throughput at several concurrency levels
- [ ] Results exist for at least two quantization levels
- [ ] Total GPU spend stays within $50–100/month

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 6: Gateway v2 (Months 4–5)

**Goal:** Route between Claude and vLLM by task and cost, with fallback, and publish the numbers.

**What to build**
- [ ] Routing between Claude and vLLM by task/cost
- [ ] Fallback when a backend fails
- [ ] Cost-crossover analysis: at what volume self-hosting beats the API
- [ ] Blog post publishing the benchmarks

**What I'll learn**
- Designing routing rules around task type and cost
- Failure detection and fallback between providers
- Cost modeling: API cost per token vs. GPU cost per hour
- Writing up technical results for a public audience

**Done when**
- [ ] Requests are routed to Claude or vLLM according to the defined rules, and metering records which backend served each one
- [ ] Taking vLLM down makes traffic fall back to Claude without client errors
- [ ] The blog post with the benchmarks and cost-crossover analysis is published

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 7: Guardrails and evals as shared services (Months 6–7)

**Goal:** Turn guardrails and evals into services other teams can use.

**What to build**
- [ ] PII redaction service
- [ ] Injection screening service
- [ ] Evals as a shared service
- [ ] Versioned APIs for each service
- [ ] Onboarding for other teams
- [ ] One merged PR to vLLM or LiteLLM (start with small issues from month 3)

**What I'll learn**
- API design for internal platform services
- API versioning without breaking consumers
- What drives adoption by other teams
- Contributing to a large open-source codebase

**Done when**
- [ ] At least one consumer other than my gateway calls the services
- [ ] A new API version ships without breaking existing consumers
- [ ] One PR is merged into vLLM or LiteLLM

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 8: LoRA fine-tune, served behind the gateway (Months 8–9)

**Goal:** Take a fine-tuned model from training to production behind the gateway.

**What to build**
- [ ] LoRA fine-tune of a small open model
- [ ] Track the run in MLflow
- [ ] Promote the model through an eval gate
- [ ] Serve it as a vLLM multi-LoRA adapter
- [ ] Route to it through the gateway

**What I'll learn**
- LoRA fine-tuning of a small open model
- Experiment tracking and model registry in MLflow
- Eval-gated model promotion
- Multi-LoRA serving in vLLM

**Done when**
- [ ] The fine-tune run and its metrics are recorded in MLflow
- [ ] A model that fails the eval gate is not promoted
- [ ] A request through the gateway is served by the LoRA adapter on vLLM

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Alongside the projects

**At work (ByteForge)**
- [ ] Bring the gateway into ByteForge for Standwise
- [ ] Bring each later project's idea into ByteForge where possible
- [ ] Write design docs
- [ ] Own cost and reliability metrics
- [ ] Mentor one engineer

**Interview prep**
- [ ] From month 3: 3–4 LeetCode medium problems per week in Python
- [ ] From month 4: weekly system design mocks
- [ ] From month 4: apply to visa-sponsoring jobs, while I keep building

**After each project**
- 30-minute note: what I built, what broke, what I'd change, one tradeoff (a checkbox is in each project's Write-up)

## If I fall behind, cut in this order

1. DDP/Airflow
2. Karpenter
3. MIG/GPU time-slicing
4. Multi-LoRA serving

**Never cut:** Gateway v2 or the benchmark blog post.

## Checkpoints

- [ ] Month 4: getting recruiter replies for AI platform/infra roles
- [ ] Month 6: reaching onsite or final rounds

If not: the likely cause is coding rounds or too little production evidence, not too little studying.

## Week 1 (Project 1, by day)

- [x] Day 1: Create the repo, set up FastAPI, add a `/health` endpoint
- [x] Day 2: Make `POST /v1/messages` forward a request to Claude and return the response
- [x] Day 3: Add a Postgres `tenants` table with hashed API keys, and reject unknown keys with a 401
- [x] Day 4: Add a `usage_events` table recording tokens, cost, and latency for every call
- [x] Day 5: Test it with a small client script and review the code
