# Lesson 03: The RAG map — from a question to a cited answer

> Project 5 (RAG over the Kubernetes docs). Modules: M10 RAG, M9 Embeddings, M7 Structured outputs, M5 Prompting.
> This is the overview. Later lessons go deep on each stage as we build it.

**One sentence:** RAG (Retrieval-Augmented Generation) means *first find the right pages in your own documents, then give only those pages to the model and make it answer from them, with citations*.

---

## 1. The problem RAG solves

🟢 **In simple words**

Claude has read a lot of the internet, but:
- it doesn't know **your** documents (a company wiki, a contract, or here, the exact current Kubernetes docs),
- what it does know may be **old** (it learned up to a cutoff date),
- when it doesn't know, it may **make something up** that sounds right (a hallucination, M1).

**Analogy: an open-book exam.** A closed-book student answers from memory, sounding confident even when wrong. An open-book student gets a librarian who hands them the 5 most relevant pages, and the rule is: "answer only from these pages, and write which page each fact came from". RAG turns the model into the open-book student. **The librarian is the hard part.** If the librarian hands over the wrong pages, even a brilliant student fails.

🔵 **Technically**

RAG = **retrieval** (a search system picks relevant chunks) + **augmentation** (those chunks are pasted into the prompt) + **generation** (the LLM writes the answer, grounded in them).

The alternatives, and when they win (M10 "long context vs RAG"):

| Option | When it's better |
|---|---|
| **Put all docs in the prompt** (long context + prompt caching) | A small, stable set: a few hundred pages at most. The Kubernetes docs are thousands of pages, so no |
| **Fine-tune the model on the docs** | Teaching *style or format*, not facts. Fine-tuned facts still get hallucinated, and can't be cited or updated easily |
| **RAG** | Large, changing document sets where you need citations, freshness and per-user access control |

---

## 2. The whole pipeline

There are two halves. **Ingestion** runs ahead of time, in a batch. **Query** runs on every question.

```
 ═══ INGESTION (offline, re-runnable) ═══════════════════════════════════════════

  kubernetes/website      ┌────────┐   ┌─────────┐   ┌───────────┐   ┌──────────────────┐
  content/en/docs/*.md ──▶│ parse  │──▶│  chunk  │──▶│   embed   │──▶│  Postgres        │
  (~1,500 Markdown files) │ + meta │   │ by      │   │ text →    │   │  chunks table:   │
                          │ (title,│   │ heading │   │ vector    │   │  text, metadata, │
                          │  URL)  │   │         │   │           │   │  vector (pgvector│
                          └────────┘   └─────────┘   └───────────┘   │  tsvector (FTS)  │
                                                                     └────────┬─────────┘
 ═══ QUERY (per question) ═════════════════════════════════════════════════════│═══════
                                                                              │
  "How do I stop a                                                            │
   rollout from    ──┬──▶ embed question ──▶ vector search  (top 50) ─┐       │
   taking down all   │                                                ├─▶ RRF │ fuse
   my pods?"         └──▶ keyword search (Postgres full-text, top 50)─┘   │   │
                                                                          ▼   │
                                              rerank: keep the best 5 ◀───┘───┘
                                                          │
                                                          ▼
                     prompt = instructions + 5 chunks (with ids) + question
                                                          │
                                                          ▼
                     Claude ── through YOUR GATEWAY (auth, quota, metering, trace)
                                                          │
                                                          ▼
                     structured answer {answer, citations: [chunk ids]}
                     → validate (Pydantic; do the cited ids exist?) → user
```

**Every model call goes through your gateway.** So the RAG app is just one more tenant: it gets a key, a quota, metered cost and traces for free. That's the platform you built, now being used.

---

## 3. Embeddings: search by meaning

🟢 **In simple words**

An **embedding** turns text into a list of numbers, like **coordinates on a map of meanings**. Texts that mean similar things land close together, even with no words in common. "Stop a rollout from taking down all my pods" lands near the page about **PodDisruptionBudget**, though they share no words. Searching then means "find the stored points nearest to the question's point".

🔵 **Technically**

- An **embedding model** (a smaller neural network, not Claude) maps text → a fixed-length vector, e.g. 384 or 1024 numbers.
- **Cosine similarity** compares two vectors by their *angle*: 1 = same direction (same meaning), 0 = unrelated.
  `cos(a, b) = (a · b) / (|a| × |b|)`
- **pgvector** is a Postgres extension that adds a `vector` column type, distance operators (`<=>` = cosine distance = 1 − similarity) and an **ANN index** (approximate nearest neighbour: finds the *almost* closest vectors fast, instead of comparing against every row). The index type we'll use is **HNSW**, a graph you hop through towards the nearest points. Details in a later lesson (M9).

### Worked example A: cosine with tiny 2-D vectors

Pretend embeddings have only 2 numbers:

| Text | Vector |
|---|---|
| Question: "stop rollout killing all pods" | q = (0.8, 0.6) |
| Chunk 1: PodDisruptionBudget page | c1 = (0.9, 0.4) |
| Chunk 2: "Install kubectl on macOS" | c2 = (0.1, 0.99) |

All three already have length ≈ 1, so cosine ≈ just the dot product:
- q · c1 = 0.8×0.9 + 0.6×0.4 = 0.72 + 0.24 = **0.96**, very close in meaning
- q · c2 = 0.8×0.1 + 0.6×0.99 = 0.08 + 0.59 = **0.67**, much less related

Chunk 1 wins, with **zero shared words**. That's what keyword search can't do.

---

## 4. Why keyword search too ("hybrid")

🟢 **In simple words**

Meaning-search is fuzzy. It's great for "how do I…" questions, but it **blurs exact names**: `--max-surge`, `kube-proxy`, `ImagePullBackOff`, `v1.29`. In embedding space, `maxSurge` and `maxUnavailable` sit almost on top of each other. When a user pastes an exact error string, old-fashioned **word matching** finds it better.

🔵 **Technically**

- **Full-text search** (FTS) in Postgres: a `tsvector` column (the chunk's words, normalized) plus `ts_rank` scoring. This is similar to **BM25**, the classic keyword ranking used by search engines (rare words count more than common ones).
- **Hybrid search** runs both and merges the two ranked lists.

### Worked example B: who wins which query

| Query | Better at it | Why |
|---|---|---|
| "how do I keep my app up during node maintenance?" | Vector | Meaning, no exact terms. The answer page says "PodDisruptionBudget" and "drain", not "keep up" |
| "ImagePullBackOff" | Keyword | One exact token. Embeddings put it near every "pull/image/error" page |
| "difference between maxSurge and maxUnavailable" | Keyword finds the exact page; vector helps phrase-level matching | Exact field names. Hybrid gets both |

---

## 5. Merging two lists: Reciprocal Rank Fusion (RRF)

🟢 **In simple words**

The two searches give scores on totally different scales (cosine 0–1 vs `ts_rank` 0.0x), so you can't add the scores. RRF ignores the scores and uses only **positions**: being near the top of *either* list is good, and being near the top of *both* is best.

🔵 **Technically**

```
RRF(chunk) = Σ over lists of   1 / (k + rank in that list)        k = 60 (a standard default)
```

The `k = 60` stops the #1 spot from dominating: 1/61 vs 1/62 is a small gap.

### Worked example C: RRF by hand

| Chunk | Vector rank | Keyword rank | RRF score |
|---|---|---|---|
| A | 1 | — (not found) | 1/61 = **0.0164** |
| B | 3 | 2 | 1/63 + 1/62 = 0.0159 + 0.0161 = **0.0320** |
| C | — | 1 | 1/61 = **0.0164** |
| D | 2 | 8 | 1/62 + 1/68 = 0.0161 + 0.0147 = **0.0308** |

Final order: **B, D, then A and C** (tied). B was never #1 in either list, but it was good in *both*, and that's the signal RRF rewards.

---

## 6. Reranking: a careful second look

🟢 **In simple words**

The first search is a **fast skim** of thousands of chunks: it has to be cheap, so it's rough. Reranking is a **careful read** of only the top ~50: a slower model looks at the question and each chunk *together* and scores how well that chunk answers it. You keep the best 5.

**Analogy:** a recruiter skims 1,000 CVs for keywords (fast, rough), then a hiring manager reads the 50 shortlisted carefully (slow, accurate).

🔵 **Technically**

- The embedding search is a **bi-encoder**: question and chunk are embedded *separately*, so chunk vectors can be computed once, in advance. Fast, but the two never "see" each other.
- A reranker is a **cross-encoder**: it reads `(question, chunk)` as one input and outputs a relevance score. More accurate, but it must run per pair at query time, so it's only affordable on a shortlist.
- Why not send all 50 to Claude? Cost (50 chunks ≈ 25k input tokens per question), latency (M2: more input means slower first token), and **lost in the middle** (M1: models use the middle of long contexts less reliably).

---

## 7. Chunking: cutting docs into pieces

🟢 **In simple words**

You can't embed a whole 30-page doc as one point: its meaning would be an average of 20 topics, close to nothing in particular. You also can't use single sentences: "Set it to 2." means nothing on its own. **A chunk should be one self-contained idea**, usually a section under one heading.

🔵 **Technically**

- **Split by Markdown heading** (`##`, `###`), since the Kubernetes docs are well structured. Cap the size (e.g. ~500–800 tokens) and split long sections with a small overlap.
- **Keep metadata** on each chunk: page title, section path ("Concepts > Workloads > Deployments > Rolling update"), source URL. It's needed for citations, filters, and debugging.
- **Prefix the section path to the text before embedding.** A chunk saying "Set `maxUnavailable` to 1…" means more when it starts with "Deployments > Rolling Update Deployment:". (That's a light version of "contextual retrieval", M10.)
- **Stable chunk IDs** (e.g. a hash of file path + heading + text) make re-running ingestion **idempotent**: no duplicates, and unchanged chunks are skipped instead of re-embedded.

### Worked example D: one page → chunks

```
concepts/workloads/controllers/deployment.md
 ├ # Deployments                      → chunk 1  "Deployments: A Deployment provides declarative updates…"
 ├ ## Creating a Deployment           → chunk 2  "Deployments > Creating a Deployment: The following…"
 ├ ## Updating a Deployment           → chunk 3
 │   └ ### Rollover                   → chunk 4  (own heading = own chunk)
 └ ## Writing a Deployment Spec
     └ ### Strategy
         └ #### Rolling Update        → chunk 5  "Deployments > … > Rolling Update: maxUnavailable is…"
             (1,400 tokens: too big)  → chunk 5a, 5b with ~50 tokens of overlap
```

---

## 8. Generation: grounded, cited, checkable

🟢 **In simple words**

The prompt says: "Here are 5 numbered excerpts. Answer **only** from them. After each fact, cite the excerpt. If they don't contain the answer, say you don't know." Then your **code** checks the answer before showing it. You don't trust the model's word that it followed the rules.

🔵 **Technically**

- Chunks go in the prompt wrapped in tags with IDs (`<doc id="c_81f2">…</doc>`), documents first and the question last (M5: long documents before the question).
- **Structured output** (M7): the model returns JSON matching a schema, e.g. `{answer: str, citations: [chunk_id], answerable: bool}`. **Pydantic** (a Python library that checks data against a declared shape) validates it. On failure, send the error back and retry once, then fall back safely.
- **Semantic check in code:** every cited ID must be one of the 5 we sent. A made-up ID is a hallucination you can catch for free.
- **"I don't know" is a feature.** `answerable: false` for questions the docs don't cover is far better than a confident invention.

---

## 9. Where RAG fails (preview of Project 6)

```
 wrong answer
     │
     ├── was the right chunk in the top 5?  ── NO ──▶ RETRIEVAL failure
     │                                                (chunking, embeddings, hybrid, rerank)
     └── YES ──▶ did the answer stick to it? ── NO ──▶ GENERATION failure
                                                      (prompt, model, grounding)
```

Most real RAG problems are **retrieval failures** (M10). That's why you'll measure retrieval separately (recall@k: "was the right chunk in the top k?") before touching the prompt.

---

## 10. Proposed design and the decisions you need to make

| Part | Proposal | Why |
|---|---|---|
| Data | Kubernetes docs, `content/en/docs` (English), cloned at a pinned commit | Reproducible: evals in Project 6 need the corpus to stay fixed |
| Store | A `chunks` table in the **Postgres you already run**, with pgvector + a `tsvector` column | No new database to operate. Vector and keyword search in one SQL query |
| Answers | Claude Haiku through the gateway, with its own tenant key | Cheap, and every call is metered and traced by your own platform |
| Chunking | By heading, ~500–800 tokens, section-path prefix, stable IDs | Section 7 |
| Hybrid | Vector top 50 + FTS top 50 → RRF | Section 5 |
| Rerank | Later step (it's on the "cut first" list) | Get a baseline before optimizing |

**Decision 1: where does the code live?**
- **(Recommended)** `llm-gateway/rag/`: a new package in the same project, with its libraries in a separate uv **dependency group** (`[dependency-groups] rag = [...]`). The gateway's Docker build installs only the main dependencies, so the RAG libraries never enter the gateway image. One repo, one venv, clean separation.
- Or a separate folder next to `llm-gateway/`, with its own `pyproject.toml`. Cleaner if it ever deploys separately, but it's a second project to keep in sync, and it goes against "everything inside llm-gateway/".

**Decision 2: which embedding model?** (Claude has no embedding model of its own, so this is a separate choice.)
- **(Recommended) A small open model running locally on CPU** (e.g. `bge-small-en-v1.5`, 384 numbers per vector, via the `fastembed` library). $0, no new API key, and it embeds the whole corpus in minutes. Gives up some quality compared to the best paid models.
- **A paid embedding API** (e.g. Voyage AI, which Anthropic recommends, or OpenAI). Usually better retrieval quality. Costs a little, needs another key, and your gateway doesn't proxy embeddings, so those calls aren't metered by your platform.
- Trade-off: start local and free, and let Project 6's evals tell you whether a better embedding model is worth paying for. That's "prove it with data" instead of guessing.

---

## 11. Self-check quiz

1. Why can't you just fine-tune Claude on the Kubernetes docs instead of using RAG? Give two reasons.
2. Using 2-D vectors: question q = (0.6, 0.8), chunk X = (0.8, 0.6), chunk Y = (0.0, 1.0). Which is closer by cosine (all have length 1)?
3. A user pastes `CrashLoopBackOff`. Which search finds the right page more reliably, and why?
4. RRF with k = 60: chunk P is rank 2 in vector and rank 4 in keyword; chunk Q is rank 1 in vector only. Which ranks higher?
5. Why rerank 50 chunks down to 5 instead of sending all 50 to Claude? Give three reasons.
6. The model's answer cites `c_9999`, which wasn't among the 5 chunks you sent. What happened, and what does your code do?
7. A wrong answer: the right chunk *was* in the top 5. Retrieval failure or generation failure?

<details><summary>Answers</summary>

1. Fine-tuning teaches style/format, not reliable facts. It still hallucinates, can't cite sources, and needs re-training whenever the docs change. (Section 1 table.)
2. q·X = 0.48 + 0.48 = **0.96**; q·Y = 0 + 0.8 = **0.80**. X is closer. (Example A.)
3. Keyword (full-text) search: it's an exact token, and embeddings blur it with similar error pages. (Section 4, example B.)
4. P = 1/62 + 1/64 = 0.0161 + 0.0156 = **0.0317**; Q = 1/61 = **0.0164**. P wins. Good in both lists beats #1 in one. (Example C.)
5. Cost (many more input tokens), latency (bigger prefill → slower first token), and "lost in the middle" (worse use of long context). (Section 6.)
6. A hallucinated citation. The ID check in code catches it: retry once with the error, else return a safe "couldn't produce a grounded answer". Never show it as if it were cited. (Section 8.)
7. Generation failure. The librarian did the job; the student didn't use the pages. (Section 9.)

</details>

---

## Key terms

| Simple | Technical |
|---|---|
| Open-book answering | RAG (retrieval-augmented generation) |
| Coordinates on a map of meaning | Embedding (vector) |
| How close two meanings are | Cosine similarity |
| "Vectors in Postgres" | pgvector |
| Fast "almost nearest" lookup | ANN index (HNSW) |
| Word-matching search | Full-text search / BM25 |
| Both searches together | Hybrid search |
| Merge lists by position | Reciprocal Rank Fusion (RRF) |
| Careful second look at a shortlist | Reranking (cross-encoder) |
| Embed question and doc separately | Bi-encoder |
| A self-contained piece of a doc | Chunk |
| Answer only from the given pages | Grounding |
| A made-up fact | Hallucination |
| JSON in an exact shape, checked by code | Structured output + validation |
