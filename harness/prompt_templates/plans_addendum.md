## Active Plan Syntax

Plan operations use `work_project_plan` with an `op` field: create, brief,
advance, list. Plans live at `workspace/projects/<project>/plans/<plan_id>.yaml`
with a sibling `<plan_id>.run-state.json`.

    work: project.plan({
      "op": "create",
      "project": "auth-redesign",
      "plan_id": "token-refresh",
      "purpose": "Implement offline-capable token refresh",
      "phases": [
        {"title": "Schema design", "postconditions": [
          "migrations/003_token_tables.sql exists",
          "grep:refresh_interval::models/token.py"
        ]},
        {"title": "Refresh endpoint", "postconditions": [
          "test:pytest tests/test_token_refresh.py"
        ]},
        {"title": "Offline sync", "requires_approval": true}
      ],
      "budget": {"max_sessions": 4, "deadline": "2026-05-01"}
    })

    work: project.plan({"op": "brief", "project": "auth-redesign", "plan_id": "token-refresh"})
    work: project.plan({"op": "advance", "project": "auth-redesign",
                         "plan_id": "token-refresh",
                         "action": "complete",
                         "checkpoint": "Schema landed in migration 003"})
    work: project.plan({"op": "advance", "project": "auth-redesign",
                         "plan_id": "token-refresh",
                         "action": "fail",
                         "reason": "conflicts with existing session table"})
    work: project.plan({"op": "list", "project": "auth-redesign"})

**Postcondition prefixes** (create phases):

- `grep:<pattern>::<path>` — regex search; passes when re.search finds a match.
- `test:<command>` — shell command; passes on exit code 0 (timeout 120s).
- (no prefix) — manual check, narrative reminder; not auto-verified.

**Verify before complete.** Pass `verify: true` on advance to run the
automated checks; the phase stays in-progress and a report is returned
if any grep/test check fails.

**Approval gates.** A phase with `requires_approval: true` pauses on
advance until you pass `approved: true`. The harness returns an
in-conversation message; ask the user in chat, wait for explicit OK,
then call advance again with `approved: true`.

**Failure tracking.** `action: "fail"` records a timestamped failure
with your reason but does not advance. After 3 failures on the same
phase the briefing suggests revising the plan rather than retrying.
