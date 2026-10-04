# Lesson 01: SLOs — "how reliable is reliable enough?"

> Project 4 (Monitoring), last open item. Modules: M17 Observability, M21 LLMOps.
> Read top to bottom once. The quiz at the end only asks things the worked examples showed.

**One sentence:** an SLO is a written promise like "99% of calls succeed", measured from your own metrics, that tells you when to stop shipping features and fix reliability instead.

---

## 1. Why the dashboard isn't enough

🟢 **In simple words**

Your dashboard already shows "error rate: 0.4%". But is 0.4% good or bad? Should you be woken up? Should you stop deploying? A number with no target can't answer that. Everyone looks at the same graph and has a different opinion.

An SLO is the **target line** on that graph, agreed in advance. Above the line: fine, ship features. Below the line: reliability work comes first. It turns arguments into a rule.

**Analogy: a bus company.** It doesn't promise "every bus is on time" (impossible, there's traffic). It promises "99 of every 100 buses arrive within 5 minutes of schedule, measured over a month". That's measurable, honest, and it tells the manager when to act.

🔵 **Technically**

Three words that get mixed up:

| Term | Simple meaning | Example for your gateway |
|---|---|---|
| **SLI** (Service Level *Indicator*) | The **measurement**: good events ÷ all events | `successful calls ÷ all calls` |
| **SLO** (Service Level *Objective*) | The **target** for that measurement, over a time window | "SLI ≥ 99% over 1 day" |
| **SLA** (Service Level *Agreement*) | A **contract** with a penalty if you miss | "below 99.5% → customer gets 10% refund". You don't have one, and that's normal |

Rule of thumb: SLO is stricter than the SLA, so you notice trouble before you owe money.

```
   SLI (what you measure)     SLO (your target)      SLA (contract, optional)
  ┌──────────────────────┐   ┌─────────────────┐    ┌──────────────────────┐
  │ good calls / all     │──▶│  ≥ 99% / 1 day  │───▶│ ≥ 98% / month or pay │
  │ = 99.4% right now    │   │  ✅ meeting it   │    │ (looser on purpose)  │
  └──────────────────────┘   └─────────────────┘    └──────────────────────┘
```

---

## 2. The error budget: why 100% is the wrong target

🟢 **In simple words**

If the target is 99%, then **1% of calls are allowed to fail**. That 1% is your **error budget**, like a monthly spending allowance. You can "spend" it on risky things: deploys, experiments, a model upgrade. When it's used up, you stop spending: no risky changes until it refills.

Why not 100%? Because:
- Your users can't tell 99.99% from 100% (their own Wi-Fi fails more often).
- Your upstream (OpenRouter, and the model provider behind it) isn't 100% either, so you *can't* be.
- Every extra "9" costs a lot more work and slows every deploy.

🔵 **Technically**

```
error budget = (1 − SLO) × total events in the window
```

```
 SLO 99%  ──▶  budget = 1% of calls
 ┌────────────────────────────────────────────────────────────┐
 │████████████████████████████████████████████████████████░░░░│
 │            99% must succeed                         1% may │
 │                                                       fail │
 └────────────────────────────────────────────────────────────┘
                                                     ▲
                                  spent by: bad deploys, provider outages,
                                  Redis restarts, your bugs
```

**Budget policy** (the part that makes it useful, M21): write down *in advance* what happens when it runs out. For example: "budget < 0 → no feature deploys, only fixes, until it's positive again". Without a policy, the SLO is just another graph.

### Worked example A: budget in calls

SLO = 99.5% over 1 day. Yesterday the gateway got 40,000 calls.

- Budget = (1 − 0.995) × 40,000 = 0.005 × 40,000 = **200 failed calls allowed**.
- 130 calls failed → used 130/200 = **65% of budget**, 35% left. ✅ Ship as normal.
- 260 calls failed → used 130% → **budget exhausted**. SLI = 39,740 / 40,000 = 99.35% < 99.5%. ❌ Policy kicks in: freeze risky deploys.

### Worked example B: budget in time

The same idea in minutes, if traffic is steady: 99.9% over 30 days → 0.001 × 30 × 24 × 60 = **43.2 minutes** of full outage allowed per month. 99% → **7.2 hours**. Each extra 9 cuts the budget by 10×.

---

## 3. Choosing the SLI: which calls count as "bad"?

This is the hard part, and it's where interviewers dig in.

🟢 **In simple words**

The SLI should measure **what the caller experiences as "you failed me"**, not "anything that wasn't a 200". If a caller sends a wrong API key and gets a 401, the gateway did its job perfectly. That's not a failure. If the provider is down and the caller gets an error, the caller *was* failed, even though it's not your code's fault.

🔵 **Technically**

Go through every status your gateway can return (from [main.py](../../gateway/main.py)):

| Status | When it happens in your code | Bad? | Why |
|---|---|---|---|
| 200 | Normal answer | ✅ good | |
| 400 | Caller sent a bad body / unknown model (passed through from the provider) | ✅ not counted | Caller's mistake, the gateway worked |
| 401 | Unknown key | ✅ not counted | Working as designed |
| 413 | Body over 32 MB | ✅ not counted | Working as designed |
| 429 **from our limits** | `rate limit exceeded` / `monthly token quota exceeded` | ✅ not counted | We *chose* to refuse. That's the feature working |
| 429 **from the provider** | Passed through: *our* OpenRouter key hit the provider's limit | ❌ **bad** | The tenant was within their limits and still got refused |
| 500 | Crash in our code | ❌ bad | |
| 502 / 504 | Provider unreachable / too slow | ❌ bad | Not our code, but the caller was failed |
| 503 | Our Postgres or Redis is down (fail closed) | ❌ bad | We chose safety over availability; still a failure for the caller |
| 529 | Provider overloaded (passed through) | ❌ bad | Same as 502 |
| 200 **with a wrong answer** | Model hallucinated | ⚠️ invisible | The SLO can't see it. That's what evals are for (Project 6) |

Two things to notice:

1. **The 429 trap.** Your `gateway_requests_total` has `status="429"` for *both* kinds. A naive SLI "bad = 5xx" misses the provider's 429s completely. You can separate them because `gateway_limit_rejections_total` counts only *ours*:
   ```
   provider 429s = all 429s − our 429s
   ```
2. **Exclude vs count.** "Not counted" rows are removed from **both** top and bottom of the fraction. Otherwise a tenant spamming bad keys would *improve* your SLI by adding easy "successes".

```
              good calls                     200s (and 2xx)
  SLI  =  ───────────────────  =  ─────────────────────────────────────────
           calls that count        all calls − 4xx that were the caller's fault
                                            − our own 429s
```

### Worked example C: classify a day

1,000 calls: 900× 200, 40× 401, 30× our 429, 10× provider 429, 15× 529, 5× 503.

- Not counted: 40 (401) + 30 (our 429) = 70 → **930 calls count**.
- Bad: 10 + 15 + 5 = **30**.
- SLI = (930 − 30) / 930 = 900 / 930 = **96.8%**.
- The naive "5xx only" SLI on all 1,000 calls would say (1000 − 20)/1000 = 98.0%, which hides 10 refused tenants and counts the 401 spam as success.

### Worked example D: "but 529 isn't our fault!"

True, and it still counts. The SLO is about the **caller's experience**. Excluding provider errors would make your number look great during a provider outage, while every tenant is failing. Instead, you **count it and label the cause** so a postmortem can say "70% of this week's budget was provider 529s". That number is the business case for Project 9's fallback (switch to another model when one is overloaded). M21: "graceful degradation" and "fallbacks" exist *because* provider errors eat your budget.

---

## 4. A latency SLO for an LLM: measure the right thing

🟢 **In simple words**

"Fast" for a normal API means "the reply came back in 200 ms". For an LLM that doesn't work: a 2,000-word answer *should* take 40 seconds, and a 1-word answer takes 1 second. If you set "95% of calls under 10s", long answers fail your SLO even when everything is healthy, and you'd be "fixing" nothing.

What users actually feel is **how long until words start appearing**: the time to first token (TTFT, the time from sending the request until the first piece of the answer arrives). That depends on the system, not on how long the answer is.

🔵 **Technically**

- Total duration ≈ TTFT + output tokens × time-per-token (M2). The second part is mostly decided by the **caller's** `max_tokens` and the prompt, so it's a bad SLI.
- TTFT measures queueing + prefill (the model reading the prompt) + your gateway overhead. That's what you can actually affect. Your `gateway_time_to_first_token_seconds` histogram (streams only) is the right source.

**Latency SLI as a ratio:** "fraction of streams with TTFT ≤ 2s". It's still good ÷ total, so the same budget math works.

**How a histogram answers that:** a Prometheus histogram doesn't store each value. It stores **counters per bucket** ("how many were ≤ 0.5s, ≤ 1s, ≤ 2s ..."). Your buckets are `0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30`.

```
 TTFT of 100 streams          bucket counter (cumulative, "le" = less or equal)
 ─────────────────────        ──────────────────────────────────────────────
 ≤ 0.5s : 40                  le="0.5"  → 40
 0.5–1s : 35                  le="1"    → 75
 1–2s   : 20                  le="2"    → 95   ◀── good = 95
 2–3s   :  4                  le="3"    → 99
 > 3s   :  1                  le="+Inf" → 100  ◀── total = 100
                              SLI = 95 / 100 = 95%
```

### Worked example E: why the threshold must be a bucket edge

You want "TTFT ≤ 1.5s". There's no `le="1.5"` bucket. Between 1 and 2 seconds, the histogram only knows "20 calls somewhere in 1–2s". It can't tell which side of 1.5 they're on, so the answer is a guess. **Pick a threshold that is a bucket edge (1 or 2), or add 1.5 to the buckets in code.** Adding a bucket is cheap but only affects data from that deploy onward.

---

## 5. The window, and the small-traffic problem

🟢 **In simple words**

"99% over *what period*?" Real teams use 28 or 30 days, so one bad hour doesn't wreck the number but a bad week does. Your setup has two limits:
- Prometheus keeps only **3 days** (`retention: 3d` in [monitoring.yaml](../../argocd/monitoring.yaml)), and it has no disk, so a minikube restart wipes it anyway.
- Your traffic is tiny: test calls, not real users.

🔵 **Technically**

- **Window ≤ retention.** You can't compute a 30-day SLI from 3 days of data. For learning, use a **1-day window**. In an interview: "28 days in production; I used 1 day because my Prometheus has 3-day retention and no persistent storage."
- **Small numbers swing hard.** The SLI is a fraction, and fractions with small denominators jump around.

### Worked example F: 20 calls

20 calls today, 1 failed → SLI = 19/20 = **95%**. One single failure already breaks a 99% SLO, and 99% can't even be measured: with 20 calls the possible values are 100%, 95%, 90% and so on. Fixes: a longer window (more calls), or only judging the SLO once there are at least N calls (e.g. 100), or generating steady test traffic (a "synthetic probe": a script that calls the gateway every minute). On your cluster, the honest choice is to **show the SLO and budget but not alert on them**. That matches parking the burn-rate alerts.

---

## 6. How this connects to the rest of the job (M17, M21)

| Idea | Where it shows up |
|---|---|
| Budget left → deploy or not | Your canary (Project 3) could check the SLI during the 25% step instead of only a smoke test (M21 safe rollouts) |
| "Bad" calls broken down by cause | Tells you whether to fix *your* code or add a fallback (M21 reliability) |
| A trace for every bad call | When the SLI drops, you open Tempo, filter by status, and see *which step* failed (M17) |
| The 200-with-wrong-answer row | System SLOs measure "did it answer?", never "was it right?". Quality needs evals and judges on sampled traffic (M17 "monitor quality, not just uptime", Project 6) |

That last row is the key applied-AI point: **an LLM system can be 100% "up" and still useless.**

---

## 7. Annotated example: your gateway's availability SLI in PromQL

```promql
# Good ÷ counted, over the last 1 day.
(
  sum(increase(gateway_requests_total{status=~"2.."}[1d]))          # good: 2xx
)
/
(
  sum(increase(gateway_requests_total[1d]))                          # everything...
  - sum(increase(gateway_requests_total{status=~"400|401|413"}[1d])) # ...minus caller's fault
  - sum(increase(gateway_limit_rejections_total[1d]))                # ...minus OUR 429s
)
```

- `increase(x[1d])` = how much a counter went up in the last day. Counters only go up (and reset when a pod restarts), and `increase` handles the resets.
- `sum(...)` adds up all pods, tenants and models into one number.
- Remaining budget = `1 − (1 − SLI) / (1 − 0.99)`. If it shows 0.6, 60% of the budget is left.

**On the real dashboard** ([gateway-dashboard.yaml](../../k8s/gateway-dashboard.yaml), bottom row), each subtracted part is wrapped as `(... or vector(0))`. If no 413 has ever happened, there is no 413 series at all, and "total minus nothing" in PromQL gives *no result*, not "total". `or vector(0)` turns "nothing" into 0. It also has the `tenant=~"$tenant"` filter, so you can see one tenant's SLI.

Everything not excluded and not 2xx counts as bad. That includes statuses the table above doesn't list, like a 402 from OpenRouter (account out of credit): the tenant was failed, and it's on us.

---

## 8. Lab: predict, then break (when minikube is up)

Open Prometheus: `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090` → http://localhost:9090, paste the query from section 7.

| Do this | Predict first: SLI goes up, down, or same? | Why |
|---|---|---|
| Send 10 calls with a wrong key | ? | |
| Send 10 normal calls | ? | |
| `kubectl scale deploy redis -n llm-gateway --replicas=0`, send 5 calls, scale back to 1 | ? | |
| Burst past a tenant's rate limit | ? | |

<details><summary>Answers</summary>

1. **Same.** 401s are removed from the bottom of the fraction and never reach the top.
2. **Up** (or stays at 100%). More good calls in both top and bottom.
3. **Down.** Redis down → fail closed → 503. That counts as bad. Your own safety choice costs budget, which is a real trade-off.
4. **Same.** Our 429s are excluded. Refusing is the feature working.

</details>

(Argo CD's selfHeal may scale Redis back up within a few minutes. That's fine for this lab.)

---

## 9. Self-check quiz

1. SLO = 99% over 1 day. 15,000 calls today. How many may fail? 120 failed: what % of the budget is used, and what does the policy say?
2. Mark each as good / bad / not counted: 401, our 429, the provider's 429, 529, 503 from Redis being down, a 200 with a hallucinated answer.
3. Why is "95% of calls finish in under 10 seconds" a bad SLO for an LLM gateway? What do you measure instead?
4. You want "TTFT ≤ 0.75s". Your buckets are `0.5, 1, 2`. What's the problem and what are two fixes?
5. Your cluster served 12 calls today and 1 failed. The SLO is 99%. Should you freeze deploys? What would you change?
6. The provider had a 2-hour outage and used 80% of your monthly budget. Your manager says "exclude provider errors from the SLO, it's not our fault". What do you answer?

<details><summary>Answers</summary>

1. Budget = 0.01 × 15,000 = **150**. 120/150 = **80% used**, 20% left. Still within SLO: keep shipping, but carefully. (Section 2, example A.)
2. 401 → not counted. Our 429 → not counted. Provider's 429 → **bad**. 529 → **bad**. 503 Redis → **bad**. 200 hallucination → counted as **good** by the SLO, which is exactly the blind spot evals cover. (Section 3, table + example C.)
3. Total time depends on answer length, which the caller controls, so healthy long answers would "fail". Measure **TTFT** for streams. (Section 4.)
4. No bucket edge at 0.75, so the 0.5–1s bucket can't be split. Use 0.5 or 1 as the threshold, or add a 0.75 bucket in `metrics.py` (applies only to new data). (Example E.)
5. No. 11/12 = 91.7% is noise from a tiny sample. Use a longer window, require a minimum number of calls, or add a synthetic probe, and don't alert on it. (Example F.)
6. The callers were failed whether or not it was our fault, so it stays in. Keep it **labelled by cause** so the postmortem shows that provider errors used most of the budget. That's the evidence for adding a fallback model. (Example D.)

</details>

---

## Key terms

| Simple | Technical |
|---|---|
| The measurement | SLI: good events ÷ valid events |
| The target | SLO: SLI ≥ X% over window W |
| The contract with a penalty | SLA |
| Failures you're allowed | Error budget = (1 − SLO) × events |
| What to do when it's used up | Error budget policy |
| How fast the budget is being spent (parked) | Burn rate |
| Time until words appear | TTFT (time to first token) |
| Counters per "≤ X seconds" step | Histogram buckets (`le` label) |
| A script that calls you every minute | Synthetic probe |
