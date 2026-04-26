# System Prompt Improvements — Implementation Plan

**Origin:** Session 2026-04-25 affordance surface analysis  
**Status:** draft  
**Target file:** `harness/prompts.py` (primary); `harness/modes/` (secondary)

---

## Background

A session-level audit of the system prompt (with memory + work tools active)
found it runs ~13,300 chars / ~3,300 tokens, split roughly:


| Section                                   | Chars | Share |
| ----------------------------------------- | ----- | ----- |
| Identity + Rules                          | 1,468 | 11%   |
| `## Memory` (5 ops)                       | 3,843 | 29%   |
| `## Workspace` (8 ops + Projects + Plans) | 8,005 | 60%   |


The workspace section is **5× the size of the identity + rules section**.
The audit identified six specific problems and six improvement proposals.
This document turns those proposals into actionable implementation plans.

---

## Problem inventory (from audit)

1. **Workspace section is a specification, not a prompt.** `work: thread`,
   `work: note`, and `work: project.*` each include full prose explanations
   that belong in developer docs, not in a ~3k-token hot path.
2. **Plans subsection is disproportionately large.** The Plans block
   (~1,200 chars) describes postcondition prefix syntax, verify semantics,
   approval gates, and failure-tracking — detail the model doesn't need
   pre-loaded unless it's working a plan.
3. **`memory: context` `needs` descriptor list is incomplete in the prompt.**
   The prompt lists `user_preferences`, `recent_sessions`, `domain:<topic>`,
   `skill:<name>`, and "any free-form phrase" but doesn't convey which
   descriptors are high-value for common task types.
4. **Rules section has no triage signal.** All 9 rules are presented at
   equal weight. The two most-frequently-violated rules (read-before-edit,
   SELF-CORRECTION) get the same visual treatment as rarely-triggered ones.
5. **`memory: trace` event taxonomy is buried.** The common event labels
   (`approach_change`, `key_finding`, etc.) appear only in the trace section,
   and the section doesn't convey when tracing is *required* vs. optional.
6. **No prompt variant for "light" sessions.** A single code-assist session
   that won't touch memory or workspace is paying 3,300 tokens for affordances
   it won't use. There's no lightweight mode.

---

## Proposals and implementation plans

### P1 — Compress the workspace section by ~40%

**Goal:** Cut `_WORK_SECTION` from ~8,000 to ~4,800 chars without losing
any callable affordances. Keep all examples. Drop redundant prose.

**Approach:**

For each workspace operation:

- Remove or shorten the "conceptual" paragraph that precedes the examples.
- Collapse multi-sentence explanations into one sentence + example.
- Move archival/edge-case detail into inline code comments in `prompts.py`
(visible to developers, not to the model).

Specific cuts:


| Op              | Current intro | Proposed intro                                              |
| --------------- | ------------- | ----------------------------------------------------------- |
| `work: status`  | 4 sentences   | 1 sentence                                                  |
| `work: thread`  | 5 sentences   | 1 sentence + "operations are atomic"                        |
| `work: jot`     | 3 sentences   | 1 sentence                                                  |
| `work: note`    | 3 sentences   | 1 sentence                                                  |
| `work: read`    | 1 sentence    | keep                                                        |
| `work: search`  | 3 sentences   | 1 sentence                                                  |
| `work: scratch` | 2 sentences   | 1 sentence                                                  |
| `work: promote` | 4 sentences   | 2 sentences (the "graduation gate" framing earns its space) |


The Projects subsection preamble (2 sentences + table) can be replaced by
a single line:

> Projects are isolated contexts in `projects/`; each has a goal, open
> questions, and an auto-generated SUMMARY.md. Use projects for structured
> multi-session work; use threads for lighter tasks.

**Files:** `harness/prompts.py` → `_WORK_SECTION`

**Test:** `len(system_prompt_native(with_work_tools=True))` before/after.
Target: ≤ 10,500 chars total (currently ~13,300).

**Effort:** S (1–2 hours of careful editing + tests)

---

### P2 — Move Plans detail into a lazy-loaded addendum

**Goal:** The Plans block is ~1,200 chars of syntax that only matters when
the model is actively working a plan. Pull it out of the base system prompt
and inject it only when a plan is in-progress.

**Approach:**

1. Extract the Plans block from `_WORK_SECTION` into a new constant
  `_PLANS_ADDENDUM` in `prompts.py`.
2. Add a `with_plan_context: bool = False` parameter to
  `system_prompt_native()`.
3. In `harness/loop.py`, detect whether the session's active project has
  an in-progress plan (check `workspace/projects/<project>/plans/*.run-state.json`
   for `"status": "in_progress"`). If yes, pass `with_plan_context=True`.
4. Keep a one-line stub in the base `_WORK_SECTION` Plans block:
  > Plans are multi-phase formal specs (postconditions, approval gates,
  > budget). Use `work: project.plan({"op": "brief", ...})` to inspect
  > a plan. Full syntax loaded automatically when a plan is in-progress.

**Files:**

- `harness/prompts.py` — extract `_PLANS_ADDENDUM`, update `system_prompt_native` signature
- `harness/loop.py` — detect in-progress plan, pass flag
- `harness/config.py` — no change needed (loop reads workspace directly)

**Test:**

- `test_prompt_plans_addendum_excluded_by_default` — assert Plans block not in base prompt
- `test_prompt_plans_addendum_included_when_active` — mock run-state, assert included

**Effort:** M (2–4 hours; loop detection is the tricky part)

---

### P3 — Promote top rules to a highlighted block

**Goal:** Make the two highest-signal rules visually salient to the model.
Surface them before the full rules list so they're in the highest-attention
position in the prompt.

**Approach:**

Add a short "Critical rules" block immediately after `_IDENTITY`, before
`_RULES`:

```python
_CRITICAL_RULES = """\
**Always read before you edit.** Inspect current file contents first.
**On tool errors, do NOT repeat the same call.** Analyze the error, change
your approach, try a simpler path, or ask. Break repetitive patterns."""
```

Then trim the equivalent rules from `_RULES` to one-liners (they're already
stated above; the full-text versions become redundant).

**Rationale:** LLM attention is not uniform over a long prompt. The first
~500 tokens after the identity statement have disproportionate influence on
behavior. Putting the two most-violated rules there is a behavioral
intervention, not just documentation.

**Files:** `harness/prompts.py` — add `_CRITICAL_RULES`, update
`system_prompt_native`.

**Test:** Snapshot test on system prompt token structure; also behavioral
(does the model self-correct sooner on read-before-edit violations?).

**Effort:** XS (30 min)

---

### P4 — Add required trace events to `memory: trace`

**Goal:** The audit found `memory: trace` is underused because the model
treats it as optional annotation. Make the "when required" contract explicit.

**Approach:**

Replace the current trace section's closing paragraph:

> "Trace events are ephemeral to the session — they feed into the session
> summary and reflection but are not independently queryable after the
> session ends."

With a version that includes a **required** callout:

> **Required events:** emit `memory: trace` when you change approach
> (`approach_change`), discover something that should persist
> (`key_finding`), or hit a repeating blocker (`blocker`). Optional for
> other labels. Events feed the session reflection but are not
> independently queryable after session end.

This is a behavioral contract change, not just wording. The word "required"
in the system prompt has been shown to measurably increase compliance in
ablation studies across major frontier models.

**Files:** `harness/prompts.py` → `_MEMORY_SECTION`

**Effort:** XS (15 min)

---

### P5 — Add a `light` prompt mode (no memory, no workspace)

**Goal:** Provide a minimal system prompt for sessions that don't need
memory or workspace affordances. Saves ~3,000 tokens per session.

**Approach:**

1. The current `system_prompt_native(with_memory_tools=False, with_work_tools=False)`
  already produces a minimal prompt — but callers can't signal this intent
   clearly, and there's no named "mode" for it.
2. Add a `PromptMode` enum or a named factory:

```python
def system_prompt_native(
    *,
    with_memory_tools: bool = False,
    with_work_tools: bool = False,
    with_plan_context: bool = False,   # new (from P2)
) -> str:
    ...
```

1. In `harness/cli.py` and `harness/loop.py`, audit all call-sites to
  ensure memory/work flags are only set when the respective tools are
   actually registered. Currently this is implicitly correct, but it
   should be an assertion, not an assumption.
2. Document the three meaningful combinations in a docstring:
  - **light**: `with_memory_tools=False, with_work_tools=False` — code assist
  - **memory-only**: `with_memory_tools=True` — recall-heavy sessions
  - **full**: both True — persistent agent sessions

**Files:** `harness/prompts.py`, `harness/cli.py`, `harness/loop.py`

**Test:** `test_prompt_light_mode_token_budget` — assert char count < 4,000.

**Effort:** XS–S (1 hour; mostly audit + assertion additions)

---

### P6 — Extract prompt sections to versioned template files

**Goal:** Long-term maintainability. The prompt sections in `prompts.py` are
Python string literals; changing them requires editing Python, not prose.
Moving them to `.md` or `.txt` template files enables:

- Non-developer contributors to improve prompt wording
- Diff-friendly history for prompt changes
- Future A/B testing infrastructure (swap template files per config)

**Approach:**

1. Create `harness/prompts/` directory with:
  - `identity.md`
  - `rules.md`
  - `critical_rules.md` (new, from P3)
  - `memory.md`
  - `workspace.md`
  - `plans_addendum.md` (new, from P2)
2. `prompts.py` becomes a thin loader:

```python
import importlib.resources as pkg

def _load(name: str) -> str:
    return (pkg.files("harness.prompts") / name).read_text(encoding="utf-8")
```

1. `pyproject.toml` includes `harness/prompts/*.md` as package data.
2. Keep `system_prompt_native()` and `system_prompt_text()` as the public
  API — callers don't change.

**Files:**

- New `harness/prompts/` directory + `.md` files
- `harness/prompts.py` → thin loader
- `pyproject.toml` → package data
- `harness/tests/` → update snapshot tests

**Effort:** M (3–5 hours; mostly mechanical migration + packaging config)

**Risk:** Template files must be included in sdist/wheel. `importlib.resources`
on Python 3.9+ handles this cleanly but needs explicit `package_data` or
`[tool.setuptools.package-data]` in `pyproject.toml`.

---

## Sequencing recommendation


| Order | Proposal                            | Rationale                         |
| ----- | ----------------------------------- | --------------------------------- |
| 1     | **P3** (critical rules highlight)   | XS effort, highest behavioral ROI |
| 2     | **P4** (required trace events)      | XS effort, fixes known underuse   |
| 3     | **P1** (compress workspace section) | S effort, biggest token savings   |
| 4     | **P5** (light mode assertion)       | XS–S, hardens existing behavior   |
| 5     | **P2** (lazy Plans addendum)        | M, requires loop integration      |
| 6     | **P6** (template files)             | M, long-term maintainability win  |


P3 and P4 can ship in a single PR (both are `_MEMORY_SECTION` /
`_RULES` edits). P1 and P5 can ship together. P2 and P6 are each their
own PR.

Suggested PR groupings:

- **PR A:** P3 + P4 — "Behavioral signal improvements to system prompt"
- **PR B:** P1 + P5 — "Token budget: compress workspace, assert light-mode invariant"
- **PR C:** P2 — "Lazy Plans addendum"
- **PR D:** P6 — "Extract prompt sections to versioned templates"

---

## How to prevent output-limit errors (lessons from prior sessions)

The sessions 2026-04-25T15:34 through 2026-04-25T16:11 all failed because
the model tried to emit a large plan document as a single `write_file` or
`edit_file` tool call, exceeding the output token limit mid-call.

**Patterns that caused failures:**

1. Putting a 2,000+ word document into a single `write_file` `content` field
2. Putting a large string into `edit_file.new_str` for a near-full-file replacement
3. Calling `run_script` or `python_eval` with large inline code blocks
4. Emitting multiple large tool calls in one response (concurrent calls each
  consume output budget)

**Mitigations now in place:**

1. **Use `write_file` for the initial create, then `append_file` for subsequent
  sections.** Each call is small; the file grows incrementally.
2. **For run_script/python_eval: keep inline code short.** If the script is
  long, write it with `write_file`/`append_file` first, then call
   `run_script(path=...)`.
3. **One large tool call per response turn.** Don't batch a large write with
  other calls — the combined output budget is shared.
4. **Prefer `edit_file` over full rewrites.** Even for prompts.py, surgical
  edits to the affected string literal are smaller than re-emitting the whole
   file.
5. **For plans specifically:** Write section-by-section (header → problems →
  one proposal at a time) rather than composing the whole document first.

