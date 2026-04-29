# Django Models for Capturing Agent Actions in Engram Harness

## Context from Prior Session
- Files (Markdown + git) remain the **source of truth** for knowledge, skills, and activity *summaries*.
- Activity *logs* are programmatic, append-only, and suitable for replacement by DB tables.
- Postgres (via Better Base) will be used for robust search, querying, visualization, and app integration.
- Goal: Support rich querying of agent behavior, tool usage, memory interactions, traces, without duplicating the durable memory files.

This note proposes a set of Django models (to live in `backend/engram/` or `backend/base/models/agent.py` or similar) that capture different kinds of agent actions. Models follow the existing `CoreModel` pattern (TimeStampedModel with created/modified, nice repr, QuerySet subclass).

## Core Principles for Models
- **Immutable logs where possible** — most actions are events that happened. Use `on_delete=SET_NULL` or snapshot key fields for resilience.
- **Polymorphic or tagged events** — one `AgentEvent` table with a `kind` or `verb` + JSON `payload` for flexibility (like the existing `RequestEvent`).
- **Rich indexing** — GIN on JSONB, indexes on actor/session/tool/verb/timestamps for fast search.
- **Link to memory** — `memory_path` (CharField or FilePathField) to reference the canonical Markdown file in `memory/`.
- **Session affinity** — link to `AgentSession` for grouping actions in one harness run.
- **Observability** — integrate with structlog traces; store trace_id, span_id if available.
- **Search-friendly** — full-text search on content, semantic via embeddings (future pgvector), tags for filtering.

## Proposed Models

### 1. AgentSession (groups one harness invocation)
```python
class AgentSession(CoreModel):
    session_id = models.CharField(max_length=20, unique=True)  # e.g. "act-002"
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL)  # if linked
    goal = models.TextField(blank=True)  # snapshot of task/goal
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agent_sessions")
    status = models.CharField(max_length=20, choices=[("running", "Running"), ("completed", "Completed"), ("error", "Error")])
    trace_id = models.CharField(max_length=64, blank=True)  # OpenTelemetry/ structlog correlation
    total_tool_calls = models.PositiveIntegerField(default=0)
    total_tokens_in = models.PositiveIntegerField(default=0)
    total_tokens_out = models.PositiveIntegerField(default=0)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    summary_path = models.CharField(max_length=255, blank=True)  # path to memory/activity/.../SUMMARY.md

    class Meta:
        indexes = [models.Index(fields=["session_id"]), models.Index(fields=["-created"])]
```

### 2. AgentEvent (core audit log — polymorphic via verb + payload)
Inspired by `RequestEvent`. Captures *every* significant agent action.

```python
class AgentEventVerb(TextChoices):
    TOOL_CALL = "tool_call", _("Tool call")
    MEMORY_RECALL = "memory_recall", _("Memory recall")
    MEMORY_REMEMBER = "memory_remember", _("Memory remember")
    MEMORY_PROMOTE = "memory_promote", _("Promoted note to memory")
    FILE_READ = "file_read", _("Read file")
    FILE_EDIT = "file_edit", _("Edited file")
    TRACE_EVENT = "trace_event", _("Self-annotated trace event")  # e.g. key_finding, approach_change
    PLAN_ADVANCE = "plan_advance", _("Advanced project plan phase")
    PAUSE_FOR_USER = "pause_for_user", _("Paused for user input")
    ERROR = "error", _("Error occurred")
    # Add more as harness evolves: web_search, code_execution, git_commit, etc.

class AgentEvent(CoreModel):
    REPR_FIELDS = ("id", "session_id", "verb", "actor")

    session = models.ForeignKey("AgentSession", on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agent_events")  # or null for autonomous
    verb = models.CharField(max_length=32, choices=AgentEventVerb.choices)
    memory_path = models.CharField(max_length=512, blank=True)  # e.g. "knowledge/cognitive-science/..."
    tool_name = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict, blank=True)  # flexible details: query, old_str/new_str, result_summary, error, etc.
    trace_id = models.CharField(max_length=64, blank=True)
    span_id = models.CharField(max_length=32, blank=True)
    success = models.BooleanField(default=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    objects = AgentEventQuerySet.as_manager()  # with helper methods

    class Meta:
        indexes = [
            models.Index(fields=["session", "-created"]),
            models.Index(fields=["verb", "-created"]),
            models.Index(fields=["memory_path"]),
            models.GinIndex(fields=["payload"]),  # for JSONB querying
        ]
        verbose_name = _("Agent Event")
        verbose_name_plural = _("Agent Events")
```

### 3. Supporting / Specialized Models (optional, for high-volume or complex cases)

- **AgentToolCall** (if tool calls need dedicated FKs/relations for analytics)
  - Links to `AgentEvent`, has `tool_name`, `arguments` (JSON), `result` (JSON or text), `cost_estimate`.

- **MemoryAccessLog** (for ACCESS.jsonl migration)
  - `memory_path`, `access_type` (recall/review/context), `session`, `hit_count`, `trust_level_at_access`.

- **AgentTraceEvent** (for self-annotations like `memory: trace({"event": "key_finding", ...})`)
  - Subset or view on `AgentEvent` where verb=TRACE_EVENT. Stores `event_label`, `reason`, `detail`.

- **ProjectPlanPhase** (to track harness-managed plans)
  - Links to project, phase title, status, postconditions (JSON or related model), requires_approval, completed_at.

## Usage in Harness
- On every tool call, memory op, trace, etc., the harness (or a Django management command/op) records an `AgentEvent` inside a transaction if possible.
- For file-based summaries: After session, generate/update the Markdown SUMMARY.md from aggregated `AgentEvent` queries (e.g. "X tool calls, Y memory recalls, key findings: ...").
- Search: Use Django's full-text search, JSONField lookups, or integrate with pgvector for semantic search on payload content.
- Visualization: Frontend "Engram Explorer" can query these models to show timelines, heatmaps, memory graphs, etc.

## Benefits
- Robust search and filtering on agent behavior (e.g. "all memory_promote in last 30 days for project X").
- Analytics on tool usage, error rates, cost per session.
- Keeps files as canonical for human-readable knowledge while DB handles logs/queries.
- Extensible: New verbs added easily without schema changes.
- Aligns with existing patterns (`RequestEvent`, CoreModel, structlog, transactions).

## Next Steps
- Create `backend/engram/models.py` or extend `base/models/`.
- Add to query_runner so agents can introspect these models safely.
- Implement recording hooks in the harness tool implementations.
- Add admin views and serializers for the demo "Engram Explorer" page.
- Consider partitioning or archiving old events for scale.

This design captures the different kinds of agent actions (tool, memory, trace, edit, plan, error, pause) while supporting the hybrid files + DB philosophy.

Review and refine before implementation.
