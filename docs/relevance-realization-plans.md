# Relevance Realization — Implementation Plans

**Date:** 2026-05-05
**Status:** proposed
**Context:** Motivated by the practitioner survey in
`claude-research--agent-harness-relevance-realization.md`, grounded against the
current codebase state as of the improvement-plans-2026.md update (2026-05-02,
18 of 24 themes shipped).

Four plans, ordered by dependency: Recall Eval Suite first (measures everything
else), Trust Score Decomposition second (refactors existing math), K-line
Retrieval Tagging third (novel retrieval signal), Failure Preservation fourth
(small compaction change that benefits from eval measurement).

---

## Plan 1: Recall Eval Suite

### Motivation

Every retrieval improvement — helpfulness rerank, trust decomposition, K-line
boost — is a vibe check without evals. The existing eval harness
(`harness/eval/`) tests agent loop behavior (completion, tool success, expected
tool called) but has zero coverage of the *memory retrieval path*: given a
corpus and a query, does `recall()` return the right files?

The report recommends Hamel/Shreya-style error-analysis-first evals: read
traces, taxonomize failure modes, build binary pass/fail before nuanced scales.
We adapt that to a recall-specific eval that tests the retrieval stack in
isolation — no LLM agent loop, no tool dispatch, just
`EngramMemory.recall(query) → results` against a known corpus.

### Design

#### 1.1 Recall eval corpus (fixture)

A small, self-contained Engram repo under `harness/eval/recall_fixtures/` with:

- `memory/knowledge/` — 15–25 markdown files with frontmatter (`trust`,
  `source`, `created`, `valid_from`, `valid_to`, `superseded_by`). Content
  spans 3–4 distinct topic clusters (e.g., "deployment config", "auth system",
  "data pipeline", "API design") so retrieval must discriminate.
- `memory/knowledge/ACCESS.jsonl` — synthetic access history covering a range
  of helpfulness scores and access recency. Some files heavily accessed and
  helpful (should rank high), some heavily accessed but unhelpful (should *not*
  rank high — tests the "frequency ≠ trust" distinction), some never accessed.
- `memory/knowledge/LINKS.jsonl` — a few co-retrieval edges so future K-line
  and graph-expansion tests have data.
- One or two superseded files (A2) with `valid_to` set, to verify they're
  filtered by default and retrievable with `include_superseded=True`.

The fixture is committed as static files, not generated. This makes it a
stable regression target.

#### 1.2 Recall eval task format

Extend `EvalTask` (or create a parallel `RecallEvalTask` dataclass) with
recall-specific fields:

```python
@dataclass
class RecallEvalTask:
    id: str
    query: str                    # the recall query
    k: int                        # how many results to request
    namespace: str | None         # scope (default: knowledge)
    include_superseded: bool      # default False

    # Expectations — at least one must be set
    expected_files: list[str]     # paths that MUST appear in top-k
    excluded_files: list[str]     # paths that must NOT appear in top-k
    expected_order: list[str]     # if set, these must appear in this relative order

    tags: list[str]               # for filtering (e.g. "superseded", "helpfulness", "bm25")
```

Tasks live as JSON in `harness/eval/recall_fixtures/tasks/`. Example:

```json
{
  "id": "auth-session-tokens",
  "query": "how are session tokens stored and validated",
  "k": 5,
  "expected_files": ["memory/knowledge/auth/session-tokens.md"],
  "excluded_files": ["memory/knowledge/auth/old-session-model.md"],
  "tags": ["superseded", "auth"]
}
```

#### 1.3 Recall eval runner

A new `harness/eval/recall_runner.py` that:

1. Builds an `EngramMemory` against the fixture repo (no git init needed —
   read-only is fine; `BM25Index` and optionally `EmbeddingIndex` build from
   the fixture files).
2. For each `RecallEvalTask`, calls `memory.recall(query, k=k, namespace=namespace,
   include_superseded=include_superseded)`.
3. Collects the returned file paths in order.
4. Feeds them to recall-specific scorers.

No LLM calls. No tool dispatch. Pure retrieval measurement. Fast enough to
run on every commit.

#### 1.4 Recall-specific scorers

```python
class RecallHitScorer:
    """Pass when every file in expected_files appears in the returned top-k."""
    name = "recall_hit"

class RecallExclusionScorer:
    """Pass when no file in excluded_files appears in the returned top-k."""
    name = "recall_exclusion"

class RecallOrderScorer:
    """Pass when files in expected_order appear in the correct relative order.
    Vacuously passes when expected_order is empty."""
    name = "recall_order"

class RecallMRRScorer:
    """Metric: mean reciprocal rank of expected_files. No pass/fail threshold —
    purely a metric scorer for tracking regression over time."""
    name = "recall_mrr"
```

All follow the existing `Scorer` protocol (`score(task, run) -> ScoreResult`),
with `RecallRunRecord` substituted for `RunRecord`.

#### 1.5 CLI integration

```
harness recall-eval                          # dry run (prints tasks)
harness recall-eval --really-run             # runs all tasks
harness recall-eval --really-run --tags auth # filtered
```

New `harness/cmd_recall_eval.py` subcommand registered in `harness/cli.py`.
Reuses the reporting format from `cmd_eval.py` (per-task, per-scorer, summary
line).

#### 1.6 Bootstrapping from real traces

The fixture is the stable regression target, but the highest-value evals
come from *real failure modes*. Add a utility `harness recall-eval
--from-trace <session_id>` that reads a session's `recall_candidates.jsonl`
and generates draft `RecallEvalTask` entries:

- For each recall call where the agent later used a result (helpfulness > 0.5),
  the used file becomes an `expected_file`.
- For each recall call where the agent ignored all results, the query becomes
  a candidate for manual review — "was this a retrieval failure or a query
  that shouldn't have found anything?"

This bridges the error-analysis-first method: look at real sessions, find the
failures, codify them as evals.

### Files touched

| File | Change |
|---|---|
| `harness/eval/recall_runner.py` | New — runner + `RecallEvalTask` + `RecallRunRecord` |
| `harness/eval/recall_scorers.py` | New — 4 scorer classes |
| `harness/eval/recall_fixtures/` | New directory — fixture corpus + task JSONs |
| `harness/cmd_recall_eval.py` | New — CLI subcommand |
| `harness/cli.py` | Register `recall-eval` subcommand |
| `harness/tests/test_recall_eval.py` | New — unit tests for scorers + runner against fixture |

### Complexity

Medium. ~2 PRs: fixture + runner + scorers in one; CLI integration + trace-
based task generation in a follow-up.

### Dependencies

None. Works against the existing `EngramMemory.recall()` without modification.

### Risks

- Fixture rot: if the recall path changes (new reranker, new fusion logic),
  some expected-order assertions may break legitimately. Mitigate by keeping
  order assertions sparse — most tasks should be hit/exclusion only.
- Embedding model sensitivity: `all-MiniLM-L6-v2` results vary across
  platforms. Mitigate by running recall evals in CI with a pinned model and
  accepting BM25-only fallback as a separate test path.

---

## Plan 2: Trust Score Decomposition

### Motivation

The current trust score is `trust_score(base) × decay_factor(days_since_access)`,
a two-factor product in `trust_decay.py`. The report identifies MemGuard's
6-factor decomposition as the state of the art and highlights one critical
insight: **retrieval frequency should increase urgency (how dangerous a stale
value is), not trust (how reliable it is)**. Our current design doesn't
conflate these — `access_count` gates promote/demote candidacy thresholds but
doesn't enter `effective_trust` — but there's no principled framework for
adding new signals.

The goal: decompose `effective_trust` into named, separately-tunable
components so each can be measured, ablated, and tuned independently via the
recall eval suite (Plan 1).

### Design

#### 2.1 Named component model

Replace the single `effective_trust` function with a `TrustComponents`
dataclass and a `composite_trust` function that combines them:

```python
@dataclass(frozen=True)
class TrustComponents:
    """Named, inspectable components of a file's composite trust score.

    Each component is in [0, 1]. The composite function combines them;
    the components are preserved for observability and tuning.
    """
    source_reliability: float    # base trust from frontmatter (high=1.0, medium=0.6, low=0.3)
    freshness: float             # decay_factor(days_since_last_access)
    historical_accuracy: float   # mean_helpfulness from ACCESS.jsonl
    cross_reference: float       # agreement signal from LINKS.jsonl co-retrieval density
    retrieval_urgency: float     # NOT a trust signal — high frequency = high urgency = high risk
    dependency_health: float     # are files this one supersedes/references still valid?
```

#### 2.2 Component computation

Each component maps onto existing data:

**`source_reliability`** — unchanged from `trust_score(base_trust)`. The
`_TRUST_SCORE` map stays as-is. `source: user-stated` files get a reliability
floor of 0.8 (they're human-asserted, so minimum medium-trust equivalent).

**`freshness`** — unchanged from `decay_factor(days, half_life)`. The 90-day
half-life stays as the default.

**`historical_accuracy`** — derived from `mean_helpfulness` in
`aggregate_access()`. Map the 0–1 helpfulness score directly. Files with no
access history get a neutral default of 0.5 (unknown, not unhelpful).

**`cross_reference`** — new signal from `LINKS.jsonl`. For each file, count
the number of distinct co-retrieval edges with `score ≥ threshold` (e.g.,
0.3). Normalize to [0, 1] by dividing by a cap (e.g., 10 edges → 1.0).
Files with more co-retrieval partners are corroborated by usage patterns.
Requires A3's link graph (already shipped).

**`retrieval_urgency`** — derived from `access_count` in `aggregate_access()`.
Sigmoid normalization: `1 / (1 + exp(-(count - center) / scale))` with
`center=10, scale=5` so files accessed 10+ times are flagged as high-urgency.
This is NOT a trust input — it's an *audit priority* signal. High urgency +
low accuracy = "this file is retrieved often but isn't helpful — fix it or
demote it." It enters the lifecycle advisory (`_promote_candidates.md`,
`_demote_candidates.md`) but NOT the composite trust score.

**`dependency_health`** — new signal from `superseded_by` and `valid_to`
frontmatter (A2). A file whose dependencies (referenced files, supersession
chain) are all valid scores 1.0. A file that references a superseded file
scores lower (e.g., 0.5). Derived from a one-hop walk of LINKS.jsonl
`references` and `supersedes` edges. Files with no outgoing edges score 1.0
(no dependencies to break).

#### 2.3 Composite function

```python
def composite_trust(
    components: TrustComponents,
    *,
    weights: TrustWeights | None = None,
) -> float:
    """Weighted geometric mean of trust components (excluding urgency).

    Geometric mean penalizes any single very-low component more than
    arithmetic mean — a file that's reliable but never helpful (accuracy=0.1)
    should score low overall even if freshness and source are high.

    Urgency is deliberately excluded: it's a monitoring signal, not a
    trust input.
    """
    w = weights or DEFAULT_TRUST_WEIGHTS
    factors = [
        (components.source_reliability, w.source_reliability),
        (components.freshness, w.freshness),
        (components.historical_accuracy, w.historical_accuracy),
        (components.cross_reference, w.cross_reference),
        (components.dependency_health, w.dependency_health),
    ]
    # Weighted geometric mean: prod(x_i ^ w_i) ^ (1 / sum(w_i))
    log_sum = sum(w * math.log(max(v, 1e-9)) for v, w in factors)
    weight_sum = sum(w for _, w in factors)
    return math.exp(log_sum / weight_sum) if weight_sum > 0 else 0.0
```

#### 2.4 Default weights

```python
@dataclass(frozen=True)
class TrustWeights:
    source_reliability: float = 1.0
    freshness: float = 1.0
    historical_accuracy: float = 1.5   # strongest signal — directly measures downstream value
    cross_reference: float = 0.5       # corroborative, not primary
    dependency_health: float = 0.5     # penalty for broken deps, not a primary signal
```

The 1.5× weight on historical_accuracy reflects the report's position that
actual helpfulness data is the most reliable signal available. Weights are
tunable via `TrustWeights` and ultimately via CLI flags or config.

#### 2.5 Integration points

**`effective_trust` replacement.** The existing `effective_trust(base, last_access, today)`
becomes a thin wrapper that builds `TrustComponents` from its inputs and calls
`composite_trust`. Callers that pass only `base_trust` and `last_access` (the
current signature) get the same two-factor behavior by default — the new
components default to neutral (0.5 for unknown cross-reference, 1.0 for
unknown dependency health, 0.5 for unknown accuracy).

**Backward compatibility.** `FileLifecycle` gains a `components: TrustComponents`
field alongside the existing `effective_trust: float`. The `to_dict()` method
includes the full component breakdown. `_lifecycle.jsonl` rows gain a
`"components"` key. Old rows without it are parsed as if all new components
were neutral.

**Helpfulness rerank.** `HelpfulnessIndex.reweight()` currently applies
`score × (0.5 + clamp(mean_helpfulness, 0, 1))`. After decomposition, the
reranker can optionally use the composite trust instead of raw helpfulness.
This is a follow-on — ship the decomposition first, measure via recall evals,
then decide whether composite-trust-as-reranker outperforms helpfulness-only.

**Lifecycle advisory.** `partition_candidates` thresholds switch from scalar
`effective_trust` to component-aware rules: "demote if freshness < 0.2 AND
accuracy < 0.3" is more precise than "demote if composite < 0.2." The
advisory markdown gains a component breakdown per candidate.

**Urgency reporting.** New section in `_promote_candidates.md` /
`_demote_candidates.md`: "High urgency files" — frequently retrieved files
sorted by urgency descending, flagged when urgency > 0.7 and accuracy < 0.5
("this file is used a lot and isn't helpful — review it").

### Files touched

| File | Change |
|---|---|
| `harness/_engram_fs/trust_decay.py` | Add `TrustComponents`, `TrustWeights`, `composite_trust`, `compute_components`. Preserve `effective_trust` as backward-compatible wrapper. |
| `harness/_engram_fs/link_graph.py` | Add `co_retrieval_density(file_path) -> float` helper for cross-reference component. |
| `harness/cmd_decay.py` | Update rendering to show component breakdown, add urgency section. |
| `harness/tools/memory_tools.py` | `memory_lifecycle_review` output gains component detail. |
| `harness/tests/test_trust_decay.py` | Extend with component-level tests, backward-compat tests. |

### Complexity

Medium. ~2 PRs: component model + composite function + backward-compat wrapper
in one; lifecycle advisory + urgency reporting in a follow-up.

### Dependencies

- Plan 1 (recall eval suite) should ship first so the decomposition can be
  measured. The decomposition doesn't *require* Plan 1, but without it you
  can't tell whether the new composite outperforms the old two-factor product.
- A3 (LINKS.jsonl, already shipped) for the cross-reference component.
- A2 (supersession, already shipped) for the dependency-health component.

### Risks

- Geometric mean is sensitive to near-zero components. The `max(v, 1e-9)`
  floor prevents log(0) but a file with cross-reference=0.01 (nearly
  uncorroborated) would drag the composite down even if all other signals
  are strong. Mitigate with the 0.5× weight on cross-reference and a floor
  of 0.1 on components derived from sparse data.
- Weight tuning is a rabbit hole. Ship with the defaults above, measure on
  the recall eval suite, and tune only when eval data justifies it.

---

## Plan 3: K-line Retrieval Tagging

### Motivation

The report identifies Minsky's K-line concept as "strikingly under-used":
current memory systems tag content by *what it says* (embedding similarity,
keyword overlap), not by *what reasoning configuration the agent was in when
it formed the memory*. The idea is that memories should carry metadata about
the context they were useful in — active goals, plan phase, tool sequence —
and retrieval should boost candidates whose formation context resembles the
current session's context.

This is genuinely novel. No production system implements it. The harness is
well positioned because the trace bridge already captures the formation
context for every memory: session task, tool calls, recall events, and (via
the workspace) active plan phase.

### Design

#### 3.1 Configuration vector

Define a lightweight "configuration fingerprint" that captures the reasoning
context at memory formation time:

```python
@dataclass(frozen=True)
class ConfigurationVector:
    """Lightweight fingerprint of the reasoning context when a memory was formed.

    Not an embedding — deliberately symbolic so it's inspectable,
    diffable, and doesn't require a model to compute similarity.
    """
    task_slug: str            # normalized first 8 words of the session task
    plan_phase: str | None    # active plan phase from workspace CURRENT.md, if any
    tool_sequence: tuple[str, ...]  # last N tool names used before this memory was written (N=5)
    active_namespaces: frozenset[str]  # memory namespaces the agent was reading from
    topic_tags: frozenset[str]  # extracted from the memory's heading/first paragraph (top 3 keywords)
```

The vector is symbolic, not dense. Similarity is computed as weighted Jaccard
overlap, not cosine distance. This means no model call for either writing or
querying — pure string operations.

#### 3.2 Writing configuration vectors

**When the trace bridge writes ACCESS.jsonl entries**, it already has the full
session context (task, tool sequence, active plan). Extend each ACCESS row
with a `"config"` field:

```json
{
  "file": "memory/knowledge/auth/session-tokens.md",
  "date": "2026-05-05",
  "helpfulness": 0.85,
  "session": "abc-123",
  "config": {
    "task_slug": "fix session token expiry in auth middleware",
    "plan_phase": "implementation",
    "tool_sequence": ["file_read", "file_read", "file_edit", "shell", "memory_recall"],
    "active_namespaces": ["knowledge", "skills"],
    "topic_tags": ["auth", "session", "middleware"]
  }
}
```

Note: existing ACCESS rows already include `task` and `note` fields alongside
`file`, `date`, `helpfulness`, and `session_id`. The `config` field extends
this existing row structure — no fields are removed.

```json (existing row for reference)
{
  "file": "...", "date": "...", "task": "...",
  "helpfulness": 0.85, "note": "...", "session_id": "..."
}
```

**When `memory_remember` writes a new file**, the `EngramMemory` instance
attaches the current configuration vector as sidecar frontmatter:

```yaml
---
trust: medium
source: agent-generated
created: 2026-05-05
config_task_slug: "fix session token expiry in auth middleware"
config_plan_phase: implementation
config_tool_sequence: ["file_read", "file_read", "file_edit", "shell", "memory_recall"]
config_namespaces: ["knowledge", "skills"]
config_topic_tags: ["auth", "session", "middleware"]
---
```

Frontmatter is the storage layer; it persists in git and is readable by any
consumer.

#### 3.3 Building the current session's configuration vector

`EngramMemory` builds a `ConfigurationVector` for the current session
lazily:

- `task_slug`: extracted from the session task at `start_session` time.
  Normalize: lowercase, strip punctuation, take first 8 words.
- `plan_phase`: read from `workspace/CURRENT.md` thread headers if a
  workspace is provided. If no active plan, `None`.
- `tool_sequence`: maintained as a ring buffer of the last 5 tool names,
  updated by the loop after each tool dispatch. Passed to `EngramMemory`
  via a new `update_tool_context(tool_name)` method.
- `active_namespaces`: the set of namespaces the agent has recalled from
  in this session so far. Derived from `_recall_events`.
- `topic_tags`: extracted from the query being recalled. Top 3 non-stopword
  tokens by TF (simple frequency count, no model).

#### 3.4 K-line boost at recall time

After RRF fusion and helpfulness rerank, apply a K-line boost as a third
reranking stage:

```python
def kline_boost(
    hits: list[dict],
    current_config: ConfigurationVector,
    access_configs: dict[str, list[ConfigurationVector]],  # file -> historical configs
    *,
    boost_weight: float = 0.15,
) -> list[dict]:
    """Boost hits whose historical configuration vectors overlap with the current session's.

    For each hit, find the most similar historical config vector (best match
    across all sessions that accessed this file). The similarity is weighted
    Jaccard overlap across the config dimensions. The boost is additive:

        boosted_score = base_score + (boost_weight × max_similarity)

    This means K-line context can promote a file by up to boost_weight (15%)
    of the score range, enough to break ties and promote contextually relevant
    files without overriding strong content matches.
    """
```

Similarity function:

```python
def config_similarity(a: ConfigurationVector, b: ConfigurationVector) -> float:
    """Weighted Jaccard-style similarity across symbolic config dimensions.

    Weights:
      task_slug overlap (token Jaccard):     0.30  — most discriminating
      plan_phase exact match:                0.15
      tool_sequence overlap (set Jaccard):   0.20  — captures workflow shape
      active_namespaces overlap (Jaccard):   0.10
      topic_tags overlap (Jaccard):          0.25  — captures subject proximity
    """
```

#### 3.5 Configuration vector index

For efficiency, we don't scan all ACCESS.jsonl rows on every recall. Instead:

- **Build time**: when `EngramMemory` initializes, load the ACCESS.jsonl
  rows that have `"config"` fields and build an in-memory dict:
  `file_path → list[ConfigurationVector]`. This is the same lazy-build
  pattern as `HelpfulnessIndex`.
- **Staleness**: the index is session-scoped (built once, never refreshed
  mid-session). This is fine because config vectors from the current session
  aren't useful for boosting the current session's own recall.
- **Graceful degradation**: if no ACCESS rows have config fields (old data,
  fresh corpus), the boost is a no-op. Same if embeddings are unavailable —
  the K-line boost is additive and its absence just means no config-aware
  reranking.

#### 3.6 Observability

Extend `recall_candidates.jsonl` with a `kline_similarity` field per
candidate so `harness recall-debug` can show "this file was boosted because
its historical config overlapped with the current session on task_slug and
tool_sequence." This feeds directly into the recall eval suite (Plan 1) for
measuring whether the boost helps.

### Files touched

| File | Change |
|---|---|
| `harness/_engram_fs/kline_index.py` | New — `ConfigurationVector`, `config_similarity`, `KLineIndex` |
| `harness/engram_memory.py` | Add `update_tool_context()`, build K-line index at init, apply boost in `recall()` after helpfulness rerank |
| `harness/trace_bridge.py` | Extend ACCESS row generation with `"config"` field from session snapshot |
| `harness/tools/memory_tools.py` | `memory_remember` attaches config frontmatter |
| `harness/loop.py` | Call `memory.update_tool_context(tool_name)` after each tool dispatch |
| `harness/eval/recall_fixtures/` | Add config fields to ACCESS.jsonl entries, add K-line-specific eval tasks |
| `harness/tests/test_kline_index.py` | New — similarity function tests, boost tests, graceful degradation |

### Complexity

Medium-high. ~2 PRs: config vector model + similarity + index in one;
integration into recall path + trace bridge + eval tasks in a follow-up.

### Dependencies

- Plan 1 (recall eval suite) should ship first for measurement.
- Existing ACCESS.jsonl infrastructure (already shipped).
- Existing LINKS.jsonl infrastructure (already shipped, used for
  cross_reference in Plan 2 but not directly required here).

### Risks

- **Cold start**: new corpora have no config vectors. The boost is inert
  until a few sessions have run. This is by design (graceful degradation),
  but it means the feature only shows value after some session history
  accumulates.
- **Task slug normalization**: simple word-level tokenization may produce
  poor slugs for very short or very long tasks. Mitigate with a
  configurable slug length and a normalize-to-lowercase-alpha-only filter.
- **Overfitting to workflow**: if an agent always uses the same tool
  sequence (read, read, edit, test), the tool_sequence dimension adds no
  discriminating signal. The 0.20 weight means this dimension can
  contribute at most 20% of the similarity; if it's noisy, it's diluted
  by the other dimensions.
- **Frontmatter bloat**: adding 5 config fields to every memory file's
  frontmatter adds ~200 bytes per file. Acceptable for markdown files;
  review if the corpus exceeds 10K files.

---

## Plan 4: Failure Preservation in Compaction

### Motivation

Manus's highest-leverage finding: leaving failed actions in context is more
valuable than summarizing them away, because the model uses its own dead ends
as implicit belief updates ("I already tried X and it didn't work, so I should
try Y"). The current compaction layers (B2 Layer 2 and Layer 3) don't
distinguish between successful and failed tool interactions — they summarize
everything older than the last N pairs uniformly.

The report's recommendation: failed tool calls and dead-end reasoning should
survive compaction as compact, tagged blocks rather than being flattened into
the general summary.

### Design

#### 4.1 Failure detection at compaction time

When `maybe_compact` (Layer 2) iterates over candidate tool pairs to build
the summarization prompt, classify each pair:

```python
def _is_failed_pair(assistant_msg: dict, user_msg: dict) -> bool:
    """A pair is 'failed' if any tool_result in the user message is an error,
    or if the tool output contains clear failure signals."""
    content = user_msg.get("content", [])
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("is_error"):
            return True
        text = _extract_text_from_block_content(block.get("content", ""))
        # Heuristic failure signals in non-error results
        if any(sig in text.lower() for sig in (
            "traceback", "error:", "failed", "exception",
            "no such file", "permission denied", "not found",
            "command failed", "exit code",
        )):
            return True
    return False
```

This is deliberately conservative — it only flags explicit errors and clear
failure keywords. False negatives (missed failures) are harmless (they get
summarized normally). False positives (flagged successes) are mitigated by
the compact dead-ends block being informative enough that even a
misclassified "failure" is useful context.

#### 4.2 Layer 2: compact dead-ends block

When Layer 2 compacts a region, separate the pairs into successes and failures:

```python
fresh_pairs = [...]  # existing logic
failed_pairs = [(a, u) for a, u in fresh_pairs if _is_failed_pair(messages[a], messages[u])]
success_pairs = [(a, u) for a, u in fresh_pairs if (a, u) not in failed_set]
```

The **success pairs** go through the existing summarization prompt (unchanged).

The **failed pairs** get a separate, shorter summarization with a different
prompt:

```python
_FAILURE_COMPACTION_PROMPT = (
    "You are preserving a record of failed or unsuccessful tool interactions "
    "from an agent session. These dead ends are valuable context — the agent "
    "should not re-attempt approaches that already failed.\n\n"
    "For each failed interaction below, write ONE line in this format:\n"
    "  DEAD END: <tool_name>(<key args>) → <what went wrong>\n\n"
    "Be specific: include file paths, error messages, the key argument that "
    "was wrong. Keep each line under 120 characters.\n\n"
    "===== Failed interactions =====\n\n"
    "{region_text}\n\n"
    "===== End failed interactions ====="
)
```

The dead-ends summary is inserted as a separate user message with a distinct
banner:

```python
_DEAD_ENDS_BANNER = "[harness dead ends]"

def _dead_ends_message(summary_text: str) -> dict[str, Any]:
    body = (
        f"{_DEAD_ENDS_BANNER} The following approaches were tried earlier "
        "in this session and failed. Do not re-attempt these without a "
        "different approach.\n\n"
        f"{summary_text.strip()}"
    )
    return {"role": "user", "content": body}
```

This message is placed right after the general compaction summary, before the
kept recent pairs.

#### 4.3 Layer 3: preserve dead-ends block through full compaction

When Layer 3 runs, it currently removes everything between the task and the
last N pairs. Change: if a `_DEAD_ENDS_BANNER` message exists in the region,
**preserve it** — move it into the kept section rather than summarizing it.

```python
def maybe_full_compact(...):
    # ... existing region computation ...

    # Preserve any dead-ends messages from Layer 2
    dead_end_indices = [
        i for i in range(region_start, region_end)
        if _is_dead_ends_message(messages[i])
    ]
    # Remove dead-end messages from the compaction region (they survive)
    preserved = [messages[i] for i in dead_end_indices]

    # ... existing summarization of remaining region ...

    # Insert: [L3 summary] + [preserved dead ends] + [kept recent pairs]
    messages[region_start:region_end] = [summary_msg] + preserved
```

The dead-ends block is compact (one line per failure) so it adds minimal
token cost to the L3 output. Over a 100-turn session with 10 failures, the
dead-ends block is ~1200 characters (~300 tokens) — negligible relative to
the L3 summary.

#### 4.4 Dead-ends message cap

To prevent unbounded growth in very long sessions with many failures, cap
the dead-ends block at 20 entries. When the cap is reached, drop the oldest
entries (FIFO). The cap is configurable:

```python
DEFAULT_MAX_DEAD_ENDS = 20
```

#### 4.5 Observability

Add a `dead_ends_preserved` count to the `CompactionResult` dataclass and
to the `compaction_complete` / `full_compaction_complete` trace events. This
lets the drift detector (C4) track how many dead ends are accumulating per
session — a high count may indicate the agent is thrashing.

Extend the Layer 3 full-compaction summary prompt to explicitly reference
the preserved dead-ends block: "Note: a separate [harness dead ends] message
follows this summary — it contains approaches that already failed. Do not
restate them here."

### Files touched

| File | Change |
|---|---|
| `harness/compaction.py` | Add `_is_failed_pair`, `_FAILURE_COMPACTION_PROMPT`, `_dead_ends_message`, modify `maybe_compact` to separate failures, modify `maybe_full_compact` to preserve dead-ends messages. Add `dead_ends_preserved` to `CompactionResult`. |
| `harness/tests/test_compaction.py` | Extend with failure-detection tests, dead-ends preservation through L2 and L3. |

### Complexity

Small-medium. 1 PR. The change is localized to `compaction.py` with no
downstream API changes.

### Dependencies

- None required. Benefits from Plan 1 (recall eval suite) for measuring
  whether failure preservation improves multi-turn task success, but that
  measurement is at the agent-loop eval level (existing `cmd_eval.py`), not
  the recall eval level.

### Risks

- **False positive failure detection**: the heuristic keyword list may flag
  tool results that contain the word "error" in informational context (e.g.,
  "error handling code looks correct"). Mitigate by requiring `is_error=True`
  OR a keyword match, not keyword-only for results that the API explicitly
  marked as successful. Refine the keyword list from real trace analysis.
- **Dead-ends block as distraction**: if the dead-ends block contains entries
  that are no longer relevant (the agent already worked around them), they
  add noise. Mitigate with the 20-entry cap and FIFO eviction. Long-term,
  the agent could be given a tool to dismiss dead ends it has already
  addressed.
- **Extra model call**: Layer 2 failure summarization is a separate `reflect`
  call. If failures are rare (< 3 per compaction), skip the model call and
  format the dead-ends block from the raw tool names and error messages
  directly (template-based, no LLM). The model call only fires when there
  are ≥ 3 failures to summarize. This saves cost on the common case.

---

## Cross-cutting concerns

### Sequencing

```
Plan 1 (Recall Eval Suite)
  └─→ Plan 2 (Trust Decomposition)  — measured by Plan 1
  └─→ Plan 3 (K-line Tagging)       — measured by Plan 1
Plan 4 (Failure Preservation)        — independent, measured by existing cmd_eval
```

Plan 4 can ship in parallel with Plan 1. Plans 2 and 3 should wait until
Plan 1 is running so their impact is measurable.

### Backward compatibility

All four plans are additive — no existing behavior changes unless explicitly
opted into:

- Plan 1 adds a new eval command; doesn't change the agent.
- Plan 2 preserves the existing `effective_trust` function signature; new
  components default to neutral values.
- Plan 3's K-line boost defaults to 0 contribution when no config vectors
  exist.
- Plan 4's failure detection defaults to the existing uniform-summarization
  behavior when no failures are found.

### Graceful degradation

Consistent with the project's design principle (ROADMAP §10):

- Recall eval runs without embeddings (BM25-only path).
- Trust decomposition degrades to two-factor when ACCESS/LINKS data is
  sparse.
- K-line boost is a no-op on fresh corpora.
- Failure preservation falls through to normal compaction when no failures
  detected.
