# Engram Memory & Better Base DB Migration Recommendation

**Date:** 2026-04-29
**Context:** Agent harness integration into Better Base demo ("Engram Explorer" / memory visualization as psychotechnology). Active projects: psychotechnologies-software, memory-visualization. Thread: engram-harness-better-base-demo.

## Core Principles of Engram Memory (from knowledge/)
- File/git-backed markdown with frontmatter (trust, provenance, valid_from, superseded_by, access tracking via ACCESS.jsonl).
- Namespaces: knowledge (verified facts), skills (procedures), activity (sessions), users (profiles), working (workspace).
- Consolidation pipeline: raw activity/sessions → SUMMARYs → promoted knowledge files.
- Strengths: transparency, auditability (git history), human editability, phenomenological "withdrawal" (Heidegger readiness-to-hand), semantic recall without rigid schema, value persistence across sessions.
- Tools (memory_recall, memory_review, memory_context, promote, supersede, etc.) operate on filesystem + git + embeddings.

## Better Base DB
- Postgres via Django models.
- Strong for relational queries, access patterns, user-scoped data, full-text/vector search (with extensions), integration with frontend (React components in demo).
- Current use: accounts, auth, ops, checklists, knowledge base features, etc.

## Recommendation: **Selective indexing / mirroring, not core migration**

**Do not migrate primary storage** of memory files to DB. Reasons:
- Would lose git history, easy human curation, direct file editing, lightweight deployment.
- Contradicts philosophical experiment (extended mind via readable persistent artifacts, not opaque DB rows).
- Current harness is optimized for file-based recall (semantic + keyword + lifecycle review).
- Migration would require major rewrite of recall/review/promote tools, frontmatter parsing, git integration.

**Do migrate / sync the following aspects to DB as part of harness integration:**
1. **Metadata index model** (`MemoryArtifact` or similar in a new `engram` Django app):
   - Fields: path, namespace, title, trust_level, created_at, last_accessed, access_count, semantic_hash, superseded_by.
   - Sync via post-write hooks or periodic task (Celery) that parses frontmatter and updates DB.
   - Enables fast DB queries for visualization (e.g. "show high-trust knowledge by access frequency").

2. **Access log / activity events** — already partially in ACCESS.jsonl; mirror key events to a Django model for analytics and UI timelines.

3. **Derived views for visualization** (memory-visualization project):
   - Graph nodes/edges (semantic clusters, reconsolidation links).
   - Trust decay / lifecycle candidates (from memory_lifecycle_review).
   - User-specific memory instances if demo supports multiple "agents" or users.

4. **Harness tool enhancements**:
   - Add optional `db_sync=true` param to memory_remember/promote that also creates/updates Django records.
   - New tools or MCP endpoints for "query_memory_graph" that hit DB + falls back to files.
   - Frontend Engram Explorer page queries Django API for interactive views (using Better Base design system), with "view source file" links back to git-tracked markdown.

## Benefits of Hybrid Approach
- Preserves Engram's file-first philosophy and git durability.
- Makes memory explorable as a compelling demo feature in Better Base (aligns with psychotechnologies goal: memory as cognitive tool).
- Supports active visualization project (primitives for trust, clusters, history).
- Easy to implement incrementally; source of truth remains files.
- Allows future vector embeddings in Postgres (pgvector) for improved semantic recall.

## Risks / Mitigations
- Sync drift: Use git post-commit hooks or harness guardrails on write paths. Test with existing memory_lifecycle_review.
- Performance on large stores: DB index only metadata; full content stays in files.
- Scope creep: Keep demo scoped to read/visualize + light curation; full DB migration is out of scope.

## Next Actions
- Draft detailed spec in this note or promote to knowledge/.
- Implement `engram` Django app with basic MemoryArtifact model and sync op.
- Update harness integration proposal (engram-harness-better-base-demo thread).
- Explore visualization features against this hybrid model (update memory-visualization questions).
- Test with current memory/ files in this repo's worktree mode.

This recommendation maintains fidelity to Engram's design while enabling rich integration with Better Base. It treats the DB as a high-performance **view** over the durable file store.

**Status:** Draft for review. Aligns with recent sessions on demo integration and visualization.
