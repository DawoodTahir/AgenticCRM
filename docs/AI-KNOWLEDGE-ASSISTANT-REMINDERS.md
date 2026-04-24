# AI Knowledge Assistant — architecture & reliability reminders

Persistent notes from solution-design discussions. Use this as a checklist when building each vertical slice (Monday, Gmail, Outlook, WhatsApp, Telegram, AI layer).

**Phase 1 context (requirements):** Ingest Gmail/Outlook/WhatsApp (+ Read.ai); contact intelligence DB (Airtable or similar); AI Q&A; continuous learning; UI via Telegram or WhatsApp; Hostinger VPS; token/cost discipline.

**Data sources (your scope):** Monday.com leads (~2500) + notes; 2× Gmail + 1× Outlook; WhatsApp; example questions — which leads to focus, which past clients to approach, how to grow business.

---

## 1. Thin slice (“prove value” end-to-end)

Build **one complete path** (real data → storage → one question → one UI), not all layers of everything at once.

| Layer | Pick one first |
|--------|----------------|
| **Identity** | e.g. match by email on Monday item = same contact as Gmail (`jane@acme.com`); imperfect OK at start |
| **Source** | Monday **or** one Gmail — not all connectors day one |
| **Question type** | e.g. “What should I know about this lead?” **or** “Top 10 stale leads” |
| **UI** | e.g. Telegram only first |

**Why:** Forces real decisions on **linking, sync, storage, prompting** before multiplying sources.

**Next slices:** Same template — add one source or one question type at a time.

---

## 2. Capabilities map (decide per box, not “one vendor”)

| Capability | Question it answers |
|------------|---------------------|
| **Identity & linking** | Same person across Monday / email / WhatsApp? |
| **Ingestion** | Reliable, incremental pull/webhook + retries? |
| **Storage** | Source of truth vs derived vs searchable? |
| **Intelligence** | Precomputed profiles/summaries vs on-demand RAG/agent? |
| **Query / UX** | How users ask and trust answers (sources, freshness)? |
| **Ops** | Jobs, failures, cost ceilings, observability? |

**Decision lenses (every big choice):** outcome fit · risk (ToS, OAuth) · operability on VPS · cost (tokens, storage, time).

**Ordering:** Gate risky integrations early (e.g. WhatsApp feasibility); stable sources (Monday) pair well with first slice; **get text into a store you control** before over-tuning AI.

---

## 3. Glossary (plain language)

- **Idempotency** — Same operation twice doesn’t duplicate or corrupt (unique keys + upsert).
- **Atomicity** — Related DB changes all commit or all roll back (**transaction**).
- **Consistency** — Data obeys your rules; across APIs often **eventual** (copies catch up).
- **Durability** — After COMMIT, survives crashes (Postgres yes; memory-only no).
- **Sync** — Your DB aligned with external systems over time; usually **incremental + retries**.
- **Reality** — APIs **will** fail; goal is **safe failure, retry, visibility**, not zero errors.

**Cross-system:** No single transaction across Gmail + Monday + your DB → use **idempotency, cursors, outbox, retries** to **converge**.

---

## 4. Where to implement what (stages A–H)

### A — Design (before heavy code)

- Source of truth per fact type; stable **internal** IDs; **mapping** of external IDs.
- **Unique** rule per external object: `(source, external_id)`.
- Accept **eventual consistency** between vendors; **strong** consistency *inside* one DB transaction where needed.

### B — Ingestion

- **Idempotent** writes; **unique constraint** in DB.
- **Retries + backoff**; don’t blindly retry 4xx (except 429).
- Assume **at-least-once** delivery (webhooks/pollers).
- **Cursor / watermark** — advance **only after** batch safely committed.

### C — Normalization

- **Raw** store (JSON/blob) + **canonical** tables optional but very helpful for replay/debug.
- **FKs** + constraints; **upsert** (`ON CONFLICT … DO UPDATE` in Postgres).

### D — SQL storage

- Transactions for multi-row updates; unique indexes; `source_updated_at` / `ingested_at`; soft deletes if needed.

### E — Cross-system alignment (writes to external APIs)

- **Outbox pattern:** in one transaction write domain row + `outbox` row; worker calls API and marks done / retries.

### F — Derived data (summaries, embeddings)

- Treat as **rebuildable**; idempotent jobs (e.g. `content_hash`); **SQL for reports**, **RAG** for nuance; show freshness / source IDs.

### G — Serving (bot/API)

- Read from **your DB** with freshness rules; timeouts; **request id** in logs.

### H — Operations

- Structured logs; alerts on job failure / backlog; dead-letter after N tries; short runbooks (cursor reset, replay).

---

## 5. Symptom → missing piece (quick debug)

| Symptom | Likely fix |
|---------|------------|
| Duplicates | Unique constraint + upsert |
| Wrong counts after retry | Non-idempotent logic |
| Half-updated rows | Wider **transaction** |
| Sync stuck / rewinds | Cursor updated **after** commit |
| DB vs Monday disagree | Outbox + **source of truth** rules |
| AI vs CRM conflict | Stale derived data; show **as_of**; rebuild from raw |

---

## 6. Beginner SQL focus (high leverage)

Primary keys · unique constraints · foreign keys · transactions · upsert — covers most sync + consistency needs for Phase 1.

---

## 7. First-project implementation order (suggested)

1. Design keys + truth rules  
2. Schema with PK, `UNIQUE (source, external_id)`, FKs  
3. Ingest with upsert; cursor after commit  
4. Retries/backoff on APIs  
5. Dedup enforced in DB, not only in code  
6. AI/embedding jobs idempotent and recomputable  
7. Logs + alerts on sync failure  

---

*Last updated: conversation export for ongoing design work. Extend this file as decisions land (Postgres vs Airtable, provider choices, etc.).*
