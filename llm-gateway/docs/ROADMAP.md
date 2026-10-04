# Senior Applied AI Engineer Roadmap

Backend engineer (~3.5 years, Lahore) working as an AI agent engineer at ByteForge, aiming for a **Senior Applied AI Engineer** role that sponsors a visa. 12–15 hours/week alongside a full-time job.

**How it's built:** projects 1–4 built an LLM gateway (auth, metering, quotas, deploys, metrics, traces). Projects 5–9 build an AI app that calls Claude **through that gateway**: RAG first, then evals that measure it, then an agent, then security, then cost and speed. Each project lists the modules to study alongside it, from [ai_engineering_senior_roadmap.md](ai_engineering_senior_roadmap.md) (the theory reference: M1–M30). Theory gets read only when a project needs it.

**Changed 2026-10-04:** the target moved from AI Platform Engineer to Senior Applied AI Engineer. Platform-only work (OKE, vLLM benchmarks, LoRA serving) is parked at the bottom.

## Progress

- [ ] 1. LLM gateway
- [ ] 2. Quotas and rate limits
- [ ] 3. Deploy it properly
- [ ] 4. Monitoring
- [ ] 5. RAG over the Kubernetes docs
- [ ] 6. Evals for the RAG
- [ ] 7. Agent with tools
- [ ] 8. Attack and defend
- [ ] 9. Cheap and fast

## Timeline

| Month | Project | Outcome |
|-------|---------|---------|
| 1 | 1. LLM gateway | FastAPI proxy to Claude with per-tenant keys and usage metering in Postgres |
| 1–2 | 2. Quotas and rate limits | Redis token bucket and monthly token quotas enforced per tenant |
| 2 | 3. Deploy it properly | GitOps on minikube: CI build → scan → push, Argo CD, canary rollouts |
| 2 | 4. Monitoring | Metrics, traces and one SLO for the gateway |
| 3 | 5. RAG | Hybrid search + rerank + cited, structured answers over the Kubernetes docs |
| 3–4 | 6. Evals | Labeled eval set, error analysis, calibrated LLM judge, CI eval gate |
| 4–5 | 7. Agent | Workflow first, agent only if evals justify it; tools via MCP |
| 5–6 | 8. Attack and defend | Red-team my own app; PII redaction, injection screening, permission tiers, human approval |
| 6–7 | 9. Cheap and fast | Model routing/cascade proven by evals, prompt caching, fallbacks, cost per successful answer |

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
- [ ] Interview questions out loud: M2, M21 (gateway parts)

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
- [ ] Interview questions out loud: M22, M24

---

## Project 3: Deploy it properly (Month 2)

**Goal:** Run the gateway on infrastructure I designed myself, not just deployed onto.

**Status (2026-10-04):** done on minikube. The OKE cluster and Workload Identity moved to "Parked" (blocked on OCI capacity, and platform-only for the new goal).

**What to build**
- [x] Terraform: remote state (Object Storage), done first so no paid resource is ever tracked only on my laptop
- [x] Terraform: VCN (OCI's VPC)
- [x] Argo CD deploying the gateway
- [x] CI/CD pipeline: build
- [x] CI/CD pipeline: scan
- [x] CI/CD pipeline: push
- [x] CI/CD pipeline: canary release

**Done when**
- [x] A merge to main builds, scans and pushes an image, and Argo CD rolls it out as a canary

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff

---

## Project 4: Monitoring (Month 2)

**Goal:** Know when the gateway is unhealthy before tenants notice.

**What to build**
- [x] Prometheus metrics from the gateway
- [x] Grafana dashboards
- [x] OpenTelemetry traces across the request path
- [ ] One SLO for the gateway, shown on the dashboard

**Study alongside:** M17 Observability, M21 LLMOps

**Done when**
- [x] A Grafana dashboard shows request rate, error rate, latency and token usage per tenant
- [x] One request can be followed end to end in a trace
- [ ] The dashboard shows the SLO and how much error budget is left

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff
- [ ] Interview questions out loud: M17, M21

---

## Project 5: RAG over the Kubernetes docs (Month 3)

**Goal:** Answer questions about Kubernetes from its official docs, with citations, calling Claude through my gateway.

**Data:** the Kubernetes docs (`kubernetes/website`, `content/en/docs`, CC BY 4.0). Markdown, tables, and features that changed between versions, so retrieval has real hard cases. I know Kubernetes, so I can judge answers.

**What to build**
- [ ] Ingestion: parse, chunk (by heading), keep metadata (page, section, URL), re-runnable
- [ ] Embeddings in pgvector, in the Postgres I already run
- [ ] Hybrid search: vector + Postgres full-text, merged with RRF
- [ ] Reranking of the top candidates
- [ ] Answers as structured output (answer + cited chunk IDs), validated with Pydantic, retry on failure
- [ ] Every model call goes through the gateway, so it's metered and traced

**Study alongside:** M1 LLM foundations, M5 Prompting, M7 Structured outputs, M9 Embeddings, M10 RAG, M11 Data engineering

**Done when**
- [ ] A question returns an answer with citations that point to real chunks
- [ ] "I don't know" is returned when the docs don't cover the question
- [ ] Re-running ingestion doesn't create duplicate chunks

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff
- [ ] Interview questions out loud: M1, M5, M7, M9, M10

---

## Project 6: Evals for the RAG (Months 3–4)

**Goal:** Know the RAG's failure rate with numbers, and stop regressions before merge.

**What to build**
- [ ] Eval set of ~100 labeled questions (real-style, edge, adversarial, unanswerable), versioned in Git, with a held-out part I never tune on
- [ ] Error analysis on ~100 outputs: notes → failure categories → counts
- [ ] Retrieval scores (recall@k, MRR) separate from answer scores (faithfulness, citation accuracy)
- [ ] Code checks first; an LLM judge only for what code can't check
- [ ] Judge checked against my own labels (agreement measured) before it's trusted
- [ ] Confidence intervals (bootstrap) on every score
- [ ] CI eval gate in GitHub Actions: a regression blocks the merge
- [ ] One RAG improvement proven by the evals (e.g. contextual chunk headers)

**Study alongside:** M4 ML essentials, M15 Evaluation, M16 LLM-as-judge

**Done when**
- [ ] One command prints retrieval and answer scores with confidence intervals
- [ ] A deliberately worse prompt fails CI
- [ ] I can say "this change improved X from A to B, and it's not noise"

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff
- [ ] Interview questions out loud: M4, M15, M16

---

## Project 7: Agent with tools (Months 4–5)

**Goal:** Handle questions plain RAG can't, using a workflow first and an agent only if evals show it's needed.

**What to build**
- [ ] A workflow version first (route → retrieve → answer)
- [ ] An agent loop written from scratch (no framework) with stopping limits: steps, cost, time
- [ ] Tools exposed through an MCP server (e.g. search docs, read a page, check a YAML manifest)
- [ ] Context management for long runs (trim tool results, compaction)
- [ ] Workflow vs agent compared on the Project 6 evals: success rate, cost, latency, repeat runs

**Study alongside:** M6 Context and memory, M8 Tool use, M12 Workflows vs agents, M13 Multi-agent and durable execution, M14 Frameworks and MCP

**Done when**
- [ ] The eval comparison decides workflow vs agent, with numbers
- [ ] The agent can't loop forever or overspend
- [ ] Tool-selection accuracy is measured

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff
- [ ] Interview questions out loud: M6, M8, M12, M13, M14

---

## Project 8: Attack and defend (Months 5–6)

**Goal:** Make prompt injection unable to cause damage, by design, not by prompt.

**What to build**
- [ ] Red-team set: direct and indirect injection (poisoned docs, tool results), saved as evals
- [ ] Injection screening and PII redaction in the gateway
- [ ] Permission tiers for tools; risky tools need human approval
- [ ] Output checks (no data leaks, no unsafe links)
- [ ] Data-flow map: what goes to which provider, kept how long

**Study alongside:** M18 LLM security, M19 Guardrails and human-in-the-loop, M20 Privacy and governance

**Done when**
- [ ] Every red-team case is blocked or harmless, and runs in CI
- [ ] A poisoned document can't make the agent call a risky tool without approval
- [ ] Guardrail false positives are measured on normal traffic

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff
- [ ] Interview questions out loud: M18, M19, M20

---

## Project 9: Cheap and fast (Months 6–7)

**Goal:** Cut cost and latency without losing quality, and prove it.

**What to build**
- [ ] Routing or a cascade in the gateway (Haiku first, escalate to Sonnet when checks fail)
- [ ] Prompt caching (stable prefix first)
- [ ] Batch API for offline work (eval runs, ingestion)
- [ ] Retries with backoff and fallback when a model is overloaded
- [ ] Cost per successful answer, per tenant, on the dashboard
- [ ] Latency budget per step (retrieve, rerank, model, tools)

**Study alongside:** M2 Inference mechanics, M3 Model selection, M21 LLMOps, M22 Cost, M23 Latency and UX

**Done when**
- [ ] Cost drops by a measured amount while eval scores stay within their confidence interval
- [ ] An overloaded or failing model falls back without client errors
- [ ] A cost/quality/latency chart backs the final model choice

**Write-up**
- [ ] 30-minute note: what I built, what broke, what I'd change, one tradeoff
- [ ] Interview questions out loud: M2, M3, M22, M23
- [ ] Blog post: the eval-backed cost/quality results

---

## Alongside the projects

**At work (ByteForge)**
- [ ] Bring each project's idea into Standwise where possible (evals and security first)
- [ ] Write design docs
- [ ] Own quality, cost and reliability metrics

**Interview prep**
- [ ] From month 3: 3–4 LeetCode medium problems per week in Python
- [ ] From month 4: weekly AI system design mocks (M28)
- [ ] From month 4: apply to visa-sponsoring jobs, while I keep building
- [ ] Reading only, no build: M25 Fine-tuning, M26 Self-hosted inference, M27 Multimodal, M29, M30

**After each project**
- 30-minute note + that project's interview questions out loud (checkboxes in each Write-up)

## If I fall behind, cut in this order

1. Batch API (Project 9)
2. MCP server (plain function tools instead, Project 7)
3. Reranking (Project 5)

**Never cut:** Project 6 evals, Project 8 red-teaming, the blog post.

## Checkpoints

- [ ] Month 4: getting recruiter replies for applied AI engineer roles
- [ ] Month 6: reaching onsite or final rounds

If not: the likely cause is coding rounds or too little production evidence, not too little studying.

## Parked (platform work, only if the goal changes back)

- Terraform: OKE cluster + Workload Identity (blocked on OCI capacity)
- Burn-rate alerts on the gateway SLO
- vLLM on a rented GPU, benchmarks across concurrency and quantization
- Routing between Claude and a self-hosted model, cost-crossover analysis
- LoRA fine-tune tracked in MLflow, served as a vLLM multi-LoRA adapter
- One merged PR to vLLM or LiteLLM

## Week 1 (Project 1, by day)

- [x] Day 1: Create the repo, set up FastAPI, add a `/health` endpoint
- [x] Day 2: Make `POST /v1/messages` forward a request to Claude and return the response
- [x] Day 3: Add a Postgres `tenants` table with hashed API keys, and reject unknown keys with a 401
- [x] Day 4: Add a `usage_events` table recording tokens, cost, and latency for every call
- [x] Day 5: Test it with a small client script and review the code
