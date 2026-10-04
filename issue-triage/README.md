# issue-triage

An AI issue-triage GitHub App for open-source maintainers. For a new issue it finds likely duplicates, past fixes and relevant docs, drafts a cited reply, and **posts nothing until a maintainer approves**. Every Claude call goes through [../llm-gateway](../llm-gateway), so each installed repo is a gateway tenant (its own key, quota, cost and traces).

- Design (problem, scope, architecture, plan): [docs/PROJECT.md](docs/PROJECT.md)
- Checkboxes and progress: [../llm-gateway/docs/ROADMAP.md](../llm-gateway/docs/ROADMAP.md) (Projects 5–8)

**Status:** design done; phase 0 (measuring candidate repos) next.

---

## Folder structure

✅ = exists now. Everything else is planned, and gets created only when its phase needs it (the phase is in brackets).

```
issue-triage/
├── README.md                 ✅ this file
├── pyproject.toml            ✅ dependencies (uv); a separate project from the gateway
├── .env                      secrets, git-ignored: GITHUB_TOKEN, later the gateway key
│
├── docs/
│   └── PROJECT.md            ✅ design: problem, scope, architecture, plan
│
├── scripts/                  one-off tools, run by hand
│   └── measure_repos.py      [phase 0] score candidate repos: closed issues, linked
│                             duplicates, closing PRs, maintainer response times
│
├── db/                       SQL migrations, applied in name order (same style as the gateway)
│   ├── 001_repos_issues.sql  [phase 1] repos, issues (+ duplicate / fixed-by labels)
│   ├── 002_chunks.sql        [phase 1] chunks: text, vector(384), tsvector, created_at
│   ├── 003_runs.sql          [phase 3] triage_runs: prompt version, output, tokens, cost
│   └── 004_jobs_approvals.sql[phase 5] jobs queue, approvals (idempotency key)
│
├── triage/                   the application package
│   ├── github.py             [phase 1] GitHub API client: read history; later post comments
│   ├── ingest.py             [phase 1] backfill + incremental sync by updated_at
│   ├── chunking.py           [phase 1] issues (title + body + key comments), docs (by heading)
│   ├── embed.py              [phase 1] local embedding model (bge-small via fastembed)
│   ├── retrieve.py           [phase 2] vector + full-text → RRF → rerank; repo + time filters
│   ├── draft.py              [phase 3] prompt, gateway call, Pydantic schema, citation checks
│   └── app.py                [phase 5] webhook receiver, job worker, approval, post once
│
└── evals/                    [phase 4]
    ├── datasets/             versioned eval sets mined from closed issues (+ held-out split)
    ├── run.py                replays issues time-correctly, scores with confidence intervals
    └── judge.py              LLM judge + its calibration against my labels
```

### Why it's split this way

| Folder | Holds | Rule |
|---|---|---|
| `triage/` | Code that runs in production | One file per stage (ingest → retrieve → draft → app), so each can be tested and swapped alone |
| `db/` | The database schema | Changes only by a new numbered file, never by editing an old one |
| `scripts/` | Tools I run by hand | Never imported by `triage/` |
| `evals/` | How we measure quality | Reads `triage/`, never the other way round, so production code can't "see" the test answers |
| `docs/` | Design and decisions | Updated when a decision changes |

---

## Setup (so far)

```bash
cd issue-triage
# 1. A read-only fine-grained GitHub token (public repos, no extra permissions):
echo 'GITHUB_TOKEN=github_pat_...' > .env      # git-ignored; never paste it in chat
# 2. Install dependencies (none yet):
uv sync
```

Later phases add: a local Postgres with pgvector (`docker run pgvector/pgvector:pg17`), and a gateway tenant key.
