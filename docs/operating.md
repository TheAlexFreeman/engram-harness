# Operating the Harness API Server

This is a runbook for deploying `harness serve` somewhere other than your own
laptop. The harness CLI itself doesn't need any of this — it's a single-user
local tool. This guide is only for the FastAPI server (`harness/server.py`,
`harness/cmd_serve.py`) when you expose it to other processes, other machines,
or other people.

The TL;DR for the impatient:

```bash
export HARNESS_API_TOKEN="$(openssl rand -hex 32)"
export HARNESS_WORKSPACE_ROOT=/srv/harness/workspaces
export HARNESS_MEMORY_ROOT=/srv/harness/engram-repos
export HARNESS_DB_PATH=/srv/harness/sessions.db
export HARNESS_AUDIT_LOG=/srv/harness/audit.jsonl
export HARNESS_SERVER_MAX_ACTIVE_SESSIONS=8
harness serve --host 0.0.0.0 --port 8000
```

Anything less than this on a non-loopback interface is a misconfiguration,
and the server will refuse to start.

---

## Threat model

The HTTP server is a thin wrapper around the same agent loop the CLI runs.
Once you accept a request, the server can:

- read and write any file under the configured workspace,
- shell out (with `tool_profile=full`),
- spend money on model calls,
- write to the user's Engram memory repo,
- pause indefinitely waiting for an approval reply.

You should treat the server like an SSH endpoint that can run arbitrary code:
it should never be reachable from the public internet without auth, rate
limiting, and resource caps. The defaults below assume an internal network or
a tunnel.

The harness does **not** sandbox the agent loop at the OS level. `read_only`
and `readonly_process` are policy at the tool registry, not a kernel sandbox.
If you need OS-level isolation, run the server inside a container with a
read-only root filesystem and a restricted user.

---

## Required environment for non-loopback hosts

`harness serve` enforces a minimum security posture before it will bind to any
non-loopback host (anything other than `127.0.0.1`, `localhost`, `::1`):

| Variable | Required | Purpose |
|---|---|---|
| `HARNESS_API_TOKEN` | yes | Bearer token; clients send `Authorization: Bearer <token>`. |
| `HARNESS_WORKSPACE_ROOT` | yes | Absolute path. Every session's workspace must be inside this directory. |
| `HARNESS_ALLOW_UNAUTH_LOCAL` | escape hatch | Set to `1` only when the bind interface is intentionally non-loopback but auth is provided by an external layer (mTLS proxy). |

If `HARNESS_API_TOKEN` is empty and `HARNESS_ALLOW_UNAUTH_LOCAL` is not `1`,
the server refuses to start on a non-loopback host.

The token is compared with `hmac.compare_digest`. Rotate it by setting a new
value and restarting the process; the audit log records the token-id prefix
(first 8 hex chars of a SHA-256 of the token) on every authenticated request,
so you can correlate sessions to the active key without storing the secret.

---

## Resource caps

These have safe defaults but are worth tuning per-deployment.

| Variable | Default | Effect |
|---|---|---|
| `HARNESS_SERVER_MAX_ACTIVE_SESSIONS` | `16` | Concurrent `running` / `idle` / `paused` sessions before `POST /sessions` returns HTTP 429. |
| `HARNESS_SERVER_SSE_QUEUE_MAXSIZE` | `1000` | Per-session SSE backlog before events are dropped (the drop count is reported on `done`). |
| `HARNESS_SERVER_INTERACTIVE_IDLE_TIMEOUT_SECS` | `3600` | Auto-stop interactive sessions with no inbound message for this long. |
| `HARNESS_SESSION_EVICTION_SECS` | `7200` | How long terminal sessions linger in the in-memory registry before eviction (the SessionStore copy persists). |
| `HARNESS_LANE_CAP_MAIN` | `4` | Process-wide cap on concurrent main-loop invocations. |
| `HARNESS_LANE_CAP_SUBAGENT` | `4` | Process-wide cap on concurrent subagent invocations. |

Per-IP and per-token rate limiting is handled by the `slowapi` middleware
(see [P1.1 in the improvement plan](#) — adds rate-limit envs starting with
`HARNESS_RATE_LIMIT_*`).

---

## Path containment

| Variable | Effect |
|---|---|
| `HARNESS_WORKSPACE_ROOT` | Workspaces in `POST /sessions` must resolve under this path. |
| `HARNESS_MEMORY_ROOT` | Engram repos in `POST /sessions` must resolve under this path. Falls back to `HARNESS_WORKSPACE_ROOT` if unset. |

Without these, the server will accept any absolute path the caller sends. On a
shared host that's a privilege escalation vector — set them.

A small built-in deny list (`/etc`, `/usr`, `C:/Windows`, etc.) is always
enforced regardless of the workspace/memory roots.

---

## Tool surface gating

| Variable | Default | Effect |
|---|---|---|
| `HARNESS_SERVER_ALLOW_FULL_TOOLS` | unset | When unset, `POST /sessions` rejects `tool_profile=full`. The server defaults to `no_shell` for safety. Set to `1` to allow callers to opt into the full tool surface. |

The three tool profiles, in order of authority:

- **`read_only`** — read/search/recall tools only; no Edit, Write, Bash,
  python_exec, run_script, or work-tool writes. Approval channel never
  fires; pause tool is removed.
- **`no_shell`** — all tools except Bash. Edit, Write, python_exec,
  run_script, and work-tool writes are available.
- **`full`** — every tool, including Bash. Server-gated behind
  `HARNESS_SERVER_ALLOW_FULL_TOOLS`.

Independent of profile, `--readonly-process` (CLI) / `readonly_process: true`
(API, see P1.2) further drops the harness's own persistence side effects:

- file memory becomes a no-op,
- trace bridge writes are skipped,
- `EngramMemory.commit()` becomes a no-op.

This is the right setting for a "read the workspace and tell me about it"
session that should leave no trace on disk.

---

## Approval presets

Set `HARNESS_APPROVAL_PRESET=<name>` to gate a class of tools behind an
approval channel. Built-in presets:

| Preset | Gates |
|---|---|
| `default` | The fixed `HIGH_BLAST_RADIUS_TOOLS` set (`bash`, `git`, `web_fetch` writes, `python_exec`, etc.). |
| `high-risk` | Alias for `default` (kept for backward compat). |
| `read-only` | Every mutating tool, including all `work_*` writes and `Edit`/`Write`. |
| `bash-only` | Only `bash`. |
| `paranoid` | Every mutation, plus network tools. |
| `network-deny` | `web_fetch`, `web_search`, and any other network tool. |

Operators can also load presets from a YAML file via
`HARNESS_APPROVAL_PRESET_FILE=/path/to/presets.yaml`. The file is a top-level
mapping `<preset-name>: [<tool-name>, ...]`. File-loaded names override the
built-ins on conflict — run `harness status --presets` to print the resolved
table.

The approval channel itself is selected by `HARNESS_APPROVAL_CHANNEL`:

- `cli` — pause and read from stdin (default).
- `webhook` — POST a request to `HARNESS_APPROVAL_WEBHOOK_URL` and poll.
- `disabled` — drop approvals entirely.

For server deployments, prefer `webhook` and an out-of-band approver UI.

---

## Injection defense

`HARNESS_INJECTION_CLASSIFIER_MODEL=<provider>:<model>` enables Layer 2 of
the prompt-injection defense (see `harness/safety/injection_detector.py` and
[improvement-plans-2026 §D1](improvement-plans-2026.md)). Without it, only
Layer 1 (untrusted-output markers) is in effect.

Every injection classification produces a trace event
(`injection_classification`) and an audit-log entry. The decision is
advisory — flagged outputs land in context with a warning prepended; they
are not blocked.

---

## Audit log

`HARNESS_AUDIT_LOG=/path/to/audit.jsonl` enables a structured, append-only
audit log (one JSON object per line). The harness writes to this file:

- every authenticated and rejected HTTP request,
- every `tool_profile=full` request that the server gates,
- every approval decision (allow / deny),
- every session start with `{role, tool_profile, readonly_process}`,
- every session end (with status, turns_used, cost).

The log is the canonical record of what the server did and who asked. Rotate
it with logrotate or a similar tool — the harness opens it append-only and
re-checks the path periodically.

---

## Session artifacts on disk

Per-session artifacts land under `<workspace>/.harness/<session_id>/`:

- `trace.jsonl` — JSONL trace, the one source of truth.
- `summary.md` — trace-bridge-rendered session record (only for Engram-backed
  sessions, written when the trace bridge runs).
- `reflection.md` — agent-authored or auto-generated reflection.
- `recall_candidates.jsonl` — A6 candidate-set log.
- `checkpoint.json` — present only when the session is paused (B4).

The SessionStore SQLite DB (`HARNESS_DB_PATH`) is a separate cross-cutting
index — listing, status, role, paused-checkpoint pointer.

---

## Rotating the API token

1. Generate a new token: `openssl rand -hex 32`.
2. Update the deployment env var.
3. Restart the server. In-flight sessions continue (they don't recheck the
   token), but new requests must use the new token.
4. Invalidate any external clients that still hold the old token.

The server does not support multiple active tokens at once. For a graceful
rollover, run two server processes briefly behind a load balancer.

---

## CORS

`HARNESS_CORS_ORIGINS` is a comma-separated allowlist; defaults to
`http://localhost:3000,http://localhost:5173` for the bundled dev UI. Set this
explicitly in production.

---

## Diagnostics

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Always public. Returns `{"ok": true}`. |
| `GET /sessions` | yes | List recent sessions (in-memory + SessionStore). |
| `GET /sessions/stats` | yes | Active-session counts vs. caps. |
| `GET /sessions/{id}` | yes | Single session detail + bridge status. |
| `POST /sessions/{id}/stop` | yes | Cooperative stop (sets the stop event). |

For tail-following an active session use the SSE endpoint at
`GET /sessions/{id}/events` — never long-poll the detail endpoint.
