---
created: '2026-05-02'
errors: 0
retrievals: 6
session: memory/activity/2026/05/02/act-001
session_id: act-001
source: agent-generated
tool: harness
tool_calls: 4
total_cost_usd: 0.2541
trust: medium
---

# Session act-001

**Task:** What are some tools we could give this harness for ingesting external files (from the web, third-party projects, etc.)?

- Turns: 3
- Tool calls: 4 (0 errors)
- Tokens (in/out): 71,391 / 2,662
- Cost: $0.2541

## Summary

Good — I have enough context. The harness already has `web_search` and `web_fetch` as primitives. The security work (injection vectors, trust tiers, `_unverified/` directory) is the governance layer those tools depend on. Now I can give a well-grounded answer.

---

Here's a structured taxonomy of ingestion tools worth building for the harness, organized by source type and design tradeoff:

---

## External File Ingestion Tools for the Harness

### 1. Document Fetchers

**`fetch_url` / enhanced `web_fetch`**
Already exists in primitive form. Enhancements worth adding:
- **Rendered-JS mode** — current `web_fetch` explicitly doesn't execute JS. A Playwright/Puppeteer-backed variant handles SPAs, paywalled content rendered client-side, and dashboards.
- **PDF extraction** — pipe through `pdfminer` or `pymupdf` to return structured text from PDFs at a URL or local path.
- **EPUB/ebook ingestion** — unzip + parse `content.opf` + chapter files; useful for long-form reference works.
- **Format-aware chunking** — rather than dumping raw text, return a structured representation (title, sections, metadata) that the agent can selectively load by section.

**`fetch_arxiv` / `fetch_doi`**
Specialized wrappers around arXiv's API and DOI resolution. Return structured fields: abstract, authors, publication date, full PDF URL. Saves the agent from parsing arbitrary HTML and gets clean metadata for memory frontmatter (citation source, trust level, date).

---

### 2. Repository & Codebase Ingestors

**`clone_repo`**
Clone a remote Git repo into a sandboxed temp directory. Returns a manifest (file tree, README excerpt, language breakdown) without flooding the context window. The agent can then selectively read files using existing `read_file` / `grep_workspace` on the temp path.

Key design point: **ephemeral by default** — temp clone, not written to the workspace unless explicitly promoted. This sidesteps accidental pollution of the memory store with third-party code.

**`gh_fetch`**
GitHub API wrapper: fetch a single file, a directory listing, or issue/PR threads by URL or `owner/repo/path` shorthand. More surgical than cloning; useful for pulling in a config file or a single module from a large codebase.

**`npm_readme` / `pypi_readme`**
Fetch package metadata + README from npm or PyPI registries. Useful for quickly ingesting library documentation without cloning.

---

### 3. Structured Data Ingestors

**`fetch_rss` / `fetch_atom`**
Parse a feed URL and return items as structured objects (title, link, summary, date). Enables monitoring of external sources (papers, changelogs, news) across sessions without fetching individual pages.

**`fetch_csv` / `fetch_json`**
Fetch a data URL and return a typed schema summary + row count + sample rows. The agent shouldn't be handed raw megabyte CSVs — the tool does the truncation and summarization. Optionally promote a clean schema note to memory.

**`fetch_notion` / `fetch_confluence`** *(integration tier)*
API-backed fetchers for workspace tools where the user's actual notes live. Notion's API in particular is well-structured. These cross the line from "web tool" to "integration," but they're the highest-value ingestion path for many users.

---

### 4. Local/Filesystem Ingestors

**`ingest_local_file`**
Read a file from outside the workspace (e.g., `~/Downloads/paper.pdf`, `~/code/other-project/README.md`). The harness already has `read_file` scoped to the workspace root; this explicitly crosses that boundary with user consent.

Design: requires an explicit `allow_path` in config or a HITL approval gate — don't silently expand filesystem scope.

**`watch_directory`** *(async / polling)*
Register a local path to monitor for new files between sessions. On next session start, surface a digest of what changed. This is more of a background daemon feature but fits the external-ingestion story.

---

### 5. Ingestion Pipeline Primitives

These aren't standalone tools but make the above composable:

**`chunk_document`**
Given a long text (from any of the above), split it into semantic chunks with metadata (section heading, position, character range). Returns a manifest so the agent can selectively fetch chunks by index rather than loading everything.

**`summarize_external`**
A governed wrapper: fetch → chunk → summarize each chunk → return a compact synthesis. The key design constraint here is that this output goes to `_unverified/` by default, not directly into trusted memory — mirroring the trust architecture already in Engram's security model.

**`stage_to_unverified`**
Write ingested content to `memory/_unverified/` with source metadata and a review flag. The agent doesn't promote it to trusted memory without either user review or an explicit trust-elevation step. This is the **architectural hook** all the above tools should funnel through.

---

### 6. The Governance Question (The Hard Part)

The existing Engram security files (`memetic-security-injection-vectors.md`, etc.) already identify the central tension: **persistence amplifies injection risk**. In most systems, external content has a bounded lifetime. In Engram it can be committed to git and loaded into every future session.

That argues for a tiered trust pipeline on all ingestion:

```
fetch → parse → chunk → stage (_unverified/) → [agent review + summarize] → [user approval?] → promote (knowledge/)
```

The tools themselves are less interesting than the **promotion gate**. The question isn't just "can we get this content in?" but "what does it mean for something to be trusted memory vs. staged material?" That distinction should be a first-class concept in the tool signatures — every ingestion tool should have a `trust_target` parameter that defaults to `unverified`.

---

### Priority Order (rough)

| Priority | Tool | Rationale |
|---|---|---|
| 🔥 High | `fetch_arxiv` / `fetch_doi` | Matches Alex's actual use pattern (academic papers, ideas) |
| 🔥 High | `stage_to_unverified` (pipeline primitive) | Governance hook needed before anything else ships |
| High | `chunk_document` | Solves the context-window problem for all long-form ingestors |
| Medium | `clone_repo` / `gh_fetch` | Useful for code-adjacent knowledge tasks |
| Medium | `ingest_local_file` | High user value, low implementation cost, needs path-scoping care |
| Low | `fetch_rss` | Nice for monitoring; not a core use case yet |
| Low | `fetch_notion` | High value but significant integration surface area |

The most important single thing isn't any individual tool — it's the `stage_to_unverified → review → promote` pipeline, because without it every new ingestor is a new injection vector that bypasses the trust architecture you've already built.

## Tool usage

- `memory_recall`: 2
- `work_status`: 1
- `memory_context`: 1

## Memory recall

- memory/knowledge/philosophy/phenomenology/heidegger-readiness-presence-at-hand.md ← 'external ingestion harness tools phase' (trust=medium score=0.358)
- memory/knowledge/rationalist-community/ai-discourse/industry-influence/concept-migration-rlhf-constitutional-ai-evals.md ← 'external ingestion harness tools phase' (trust=medium score=0.306)
- memory/knowledge/software-engineering/testing/integration-testing-strategies.md ← 'external ingestion harness tools phase' (trust=medium score=0.305)
- memory/knowledge/self/security/memetic-security-comparative-analysis.md ← 'context injection security unverified trust tier' (trust=low score=0.033)
- memory/knowledge/self/security/memetic-security-injection-vectors.md ← 'context injection security unverified trust tier' (trust=low score=0.032)
- memory/knowledge/self/security/memetic-security-mitigation-audit.md ← 'context injection security unverified trust tier' (trust=low score=0.032)