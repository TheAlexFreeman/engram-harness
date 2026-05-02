---
created: '2026-05-02'
errors: 0
retrievals: 5
session: memory/activity/2026/05/02/act-003
session_id: act-003
source: agent-generated
subagent_count: 1
subagent_total_cost_usd: 3.605
tool: harness
tool_calls: 4
total_cost_usd: 0.4252
trust: medium
---

# Session act-003

**Task:** Are there any of the current file-based memory features that would be better served by a Postgres DB, if this harness were integrated into a webserver?

- Turns: 4
- Tool calls: 4 (0 errors)
- Tokens (in/out): 121,780 / 3,989
- Cost: $0.4252

## Summary

I have everything I need. Let me write the answer directly.

---

Here's a structured analysis of which current file-based features would materially benefit from Postgres in a webserver context, and which should stay as files.

---

## The Core Architectural Shift

The harness was designed for a **single-agent, single-writer, single-process** workload on a developer's laptop. In a webserver you're dealing with **concurrent requests, multiple sessions, and potentially multiple users** — which invalidates several of the key assumptions that made flat files the right choice.

---

## Strong Cases for Postgres

### 1. `ACCESS.jsonl` — Helpfulness / feedback tracking
**Current:** Append-only JSONL log per namespace; full-scan aggregated once per session into a dict; deduplicated by reading the whole file before each write.

**Problem in a webserver:** Every end-of-session trace bridge run does a full file read + rewrite of `ACCESS.jsonl`. With concurrent sessions this is a race condition (the write lock serializes it, but it's still a sequential bottleneck). More critically: the `HelpfulnessIndex` needs to be rebuilt from scratch on every new session by reading every line in every namespace's `ACCESS.jsonl` — that's fine with 200 entries but slow at 20,000.

**What Postgres gives you:**
```sql
CREATE TABLE access_events (
  id          BIGSERIAL PRIMARY KEY,
  file_path   TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  date        DATE NOT NULL,
  helpfulness REAL NOT NULL,
  note        TEXT,
  UNIQUE(file_path, session_id, date)  -- dedup constraint
);
CREATE INDEX ON access_events (file_path, date);
```
- Upserts replace the read-then-deduplicate pattern: `INSERT ... ON CONFLICT DO NOTHING`
- `mean_helpfulness`, `last_access`, `access_count` become a single indexed GROUP BY query
- Multiple sessions can write concurrently with MVCC; no filesystem lock needed
- The `_session-rollups.jsonl` materializes naturally as a view or a materialized table

---

### 2. `LINKS.jsonl` — Co-retrieval graph
**Current:** Append-only JSONL of `(from, to, kind, session_id)` edges; deduplicated by reading the whole file before each append.

**Problem in a webserver:** Graph traversal over a JSONL file is O(n) for every edge lookup. If you ever want to answer "what files are co-retrieved with X?" or build a recommendation feature, it requires a full scan. The deduplication on write has the same full-scan problem as ACCESS.

**What Postgres gives you:**
```sql
CREATE TABLE memory_links (
  from_path   TEXT NOT NULL,
  to_path     TEXT NOT NULL,
  kind        TEXT NOT NULL,
  session_id  TEXT,
  UNIQUE(from_path, to_path, kind, session_id)
);
CREATE INDEX ON memory_links (from_path);
CREATE INDEX ON memory_links (to_path);
```
Graph queries become `WHERE from_path = $1` with an index. If graph traversal gets complex, the `related:` frontmatter graph could migrate here too.

---

### 3. Frontmatter metadata / trust index
**Current:** Every recall call that needs trust filtering reads each candidate file from disk, calls `read_with_frontmatter()`, extracts the trust field. There is no pre-built index of frontmatter metadata — filtering happens post-retrieval on the raw file.

**Problem in a webserver:** This is the clearest performance bottleneck at scale. If recall surfaces 50 candidates, that's 50 synchronous disk reads just to apply trust/supersession filters. This also means you can't do a simple "give me all `trust: high` files in `memory/knowledge/`" query without walking the entire directory.

**What Postgres gives you:**
```sql
CREATE TABLE memory_files (
  path           TEXT PRIMARY KEY,
  trust          TEXT NOT NULL,          -- 'high' | 'medium' | 'low'
  source         TEXT NOT NULL,
  created        DATE,
  valid_from     DATE,
  valid_to       DATE,
  superseded_by  TEXT,
  is_superseded  BOOLEAN GENERATED ALWAYS AS (...),
  mtime          REAL,                   -- for incremental sync
  namespace      TEXT                    -- 'knowledge' | 'skills' | 'users' etc.
);
```
- Trust filtering, supersession checks, and valid-window queries all become SQL `WHERE` clauses
- `memory_lifecycle_review` becomes a query over this table JOINed with `access_events` — no file walking needed
- This also replaces the `_lifecycle.jsonl` cache: the decay computation can run as a SQL query with precomputed `effective_trust`
- The `_lifecycle_thresholds.yaml` sidecar becomes a `settings` table row

---

### 4. Session index / checkpoint state
**Current:** `SessionStore` already uses SQLite internally (the subagent found a `_write_lock = threading.Lock()` guarding a session index DB). Checkpoint state is per-file JSON.

**Problem in a webserver:** SQLite is the right call for a single-process setup, but it doesn't survive horizontal scaling (multiple Gunicorn workers or containers). A webserver with > 1 process will corrupt the SQLite DB under concurrent writes.

**What Postgres gives you:** Drop-in replacement for the SQLite session store with proper connection pooling (pgBouncer or asyncpg pool). Checkpoint JSON can move into a `JSONB` column. Session listing, filtering by user, and status queries all become indexed lookups instead of full-table scans.

---

### 5. Decay sweep state (`_lifecycle.jsonl`, `_lifecycle_thresholds.yaml`)
**Current:** CLI command writes a full JSONL recomputation of all files' lifecycle state to a gitignored sidecar. Agent reads from this cache to avoid re-walking.

**Problem in a webserver:** You can't run `harness decay-sweep` as a cron job on a filesystem that may be on ephemeral storage (containers). And the cache is per-namespace, per-run — there's no way to ask "show me all files across all namespaces with effective_trust < 0.2" without running the sweep across every namespace first.

**What Postgres gives you:** `effective_trust` is a computed column (or materialized view) over `memory_files JOIN access_events`. The "decay sweep" becomes a scheduled Celery task that does a `SELECT` + `UPDATE`, not a filesystem walk + JSONL write. The advisory promote/demote reports become queries you can run on demand.

---

## Keep as Files

These features are well-matched to the filesystem and would regress if moved:

### Memory content itself (`.md` files)
Git-backed markdown is the right format for knowledge files. It's human-editable, diff-able, auditable via `git blame`, and portable. The content does not need to be in Postgres — the *metadata* about it does. The files stay; a `memory_files` table is a metadata index over those files, synced on write.

### Embedding index (`.engram/search.db`)
The current SQLite embedding store is actually already a good fit — it uses `file_path + mtime` for incremental updates and numpy dot products for similarity. In a webserver, you'd want `pgvector` to consolidate, but the SQLite approach isn't broken — it's just not shared across processes. If you're running one server process, keep it. If you scale horizontally, migrate to `pgvector`.

### BM25 index (`.engram/bm25.db`)
Same reasoning as embedding index. Single-process: keep it. Multi-process: move to a shared store (pgvector has no BM25, so you'd use a `tsvector` GIN index on Postgres, or pg_bm25/ParadeDB).

### Bootstrap files (`HOME.md`, `users/SUMMARY.md`, `activity/SUMMARY.md`)
These are small, rarely change, and are read-once at session start. They exist precisely *because* they're human-editable entry points. Keep them as files.

### Workspace (`CURRENT.md`, project notes, plans)
The workspace is per-agent-session, not multi-user shared state. Files + a filesystem lock is fine here. If you add multi-user workspaces, *then* you'd want a DB — but that's a different design problem.

---

## Summary Table

| Feature | Current | Webserver verdict | Reason |
|---|---|---|---|
| `ACCESS.jsonl` helpfulness log | Append-only JSONL, full-scan aggregated | **→ Postgres table** | Concurrent writes, slow aggregation at scale |
| `LINKS.jsonl` co-retrieval graph | Append-only JSONL, full-scan dedup | **→ Postgres table** | Graph traversal needs indexes |
| Frontmatter trust/metadata index | Per-file disk reads at query time | **→ Postgres table** | 50 disk reads per recall for filtering is untenable at scale |
| Session store / checkpoint | SQLite + threading.Lock | **→ Postgres** | SQLite breaks under multi-process |
| Decay sweep state | JSONL sidecar + YAML thresholds | **→ Computed Postgres view** | Ephemeral storage, on-demand queries |
| Memory file content | Markdown in Git | **Keep as files** | Git provenance, human-editable, no query need |
| Embedding index | SQLite `.engram/search.db` | **Keep (single-process) / pgvector (multi-process)** | Correct design, only a scale question |
| BM25 index | SQLite `.engram/bm25.db` | **Keep (single-process) / pg tsvector (multi-process)** | Same |
| Bootstrap files | Static markdown | **Keep as files** | Read-once, human-editable |
| Workspace state | Files + write lock | **Keep as files (single-user)** | No multi-user concurrency |

---

## The Minimal Migration Path

If you were integrating this into a Django/FastAPI webserver today, the highest-leverage first step would be:

1. **`memory_files` table** — metadata index over all governed memory files (path, trust, source, created, valid_from, valid_to, superseded_by, mtime). Sync on every write, query instead of reading frontmatter from disk on recall.
2. **`access_events` table** — replaces all `ACCESS.jsonl` + `_session-rollups.jsonl` files. The `HelpfulnessIndex` becomes a single SQL query.
3. **`memory_links` table** — replaces `LINKS.jsonl`.
4. Migrate `SessionStore` SQLite → Postgres connection pool.

The embedding and BM25 indexes can stay as SQLite unless you go multi-process, at which point `pgvector` + `pg tsvector` are the natural consolidation targets — especially given that Alex's stack already includes Postgres.

## Tool usage

- `memory_context`: 1
- `work_status`: 1
- `spawn_subagent`: 1
- `memory_recall`: 1

## Subagent runs

- **subagent-001** (13 turns, 30 tool calls, 2 errors, $3.6050):
  Task: 'Survey the Engram harness codebase at C:\\Users\\Owner\\code\\personal\\engram-harness to identify all file-based memory features currently implemented. I need a concrete inventory covering:\n\n1. What data'
  Tools: grep_workspace(25), list_files(2), read_file(2), glob_files(1)

## Memory recall

- memory/knowledge/software-engineering/ai-engineering/agent-configuration-and-tooling.md ← 'polyglot persistence database selection guide Engram' (trust=medium score=0.029)
- memory/knowledge/software-engineering/databases/SUMMARY.md ← 'polyglot persistence database selection guide Engram' (trust=medium score=0.028)
- memory/knowledge/software-engineering/databases/polyglot-persistence-selection-guide.md ← 'polyglot persistence database selection guide Engram' (trust=medium score=0.023)
- memory/knowledge/software-engineering/databases/vector-database-landscape-pinecone-weaviate-chroma.md ← 'polyglot persistence database selection guide Engram' (trust=medium score=0.021)
- memory/working/projects/general-knowledge-base/IN/databases-scope-note.md ← 'polyglot persistence database selection guide Engram' (trust=medium score=0.016)