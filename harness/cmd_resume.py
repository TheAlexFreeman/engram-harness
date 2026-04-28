"""``harness resume <session_id>`` — continue a paused session (B4).

Loads the checkpoint written by ``pause_for_user``, asks the user for a
reply (interactive prompt or ``--reply`` flag), embeds the reply into the
existing conversation by mutating the placeholder ``tool_result`` block,
restores ``EngramMemory`` buffered events, re-enters the loop, and lets
the trace bridge run once the resumed session ends naturally.

Same-machine, same-workspace only in v1: the checkpoint records absolute
paths to the workspace and Engram repo, and we validate they still exist
before resuming. Cross-machine resume is a deliberate follow-up (see
docs/improvement-plans-2026.md §B4 — "deferred").
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from harness.checkpoint import (
    Checkpoint,
    ResumeState,
    mutate_pause_reply,
    read_checkpoint,
    restore_loop_state,
    restore_memory_state,
)
from harness.config import (
    SessionComponents,
    SessionConfig,
    ToolProfile,
    build_session,
)
from harness.loop import run
from harness.session_index import record_completed_session
from harness.session_store import SessionStore
from harness.usage import Usage

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reply input
# ---------------------------------------------------------------------------


_INTERACTIVE_PROMPT = (
    "Type your reply, then press Ctrl-D (Unix) / Ctrl-Z + Enter (Windows) "
    "to submit. End with an empty line to finish.\n"
)


def _read_interactive_reply() -> str:
    """Read a multi-line reply from stdin.

    Reads until EOF. We display a one-line hint to stderr so it's clear what
    submission looks like. Returns the stripped text. Raises ``ValueError`` if
    the user submits empty input — better to abort and let them retry than
    to push a meaningless empty reply into the conversation.
    """
    print(_INTERACTIVE_PROMPT, file=sys.stderr, end="")
    sys.stderr.flush()
    body = sys.stdin.read()
    text = body.strip()
    if not text:
        raise ValueError("empty reply — refusing to resume with no content")
    return text


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_resume_paths(checkpoint: Checkpoint) -> str | None:
    """Return an error string if the recorded paths are no longer usable."""
    workspace = Path(checkpoint.workspace)
    if not workspace.is_dir():
        return f"workspace no longer exists: {workspace}"
    memory_repo = Path(checkpoint.memory_repo)
    if not memory_repo.is_dir():
        return f"memory repo no longer exists: {memory_repo}"
    trace_path = Path(checkpoint.trace_path)
    if not trace_path.is_file():
        return f"trace file missing: {trace_path}"
    return None


# ---------------------------------------------------------------------------
# Config reconstruction
# ---------------------------------------------------------------------------


def _config_from_checkpoint(checkpoint: Checkpoint) -> SessionConfig:
    """Rebuild a ``SessionConfig`` that mirrors the original session.

    Only fields that affect the model + tool wiring are preserved. Loop
    bounds, budgets, and trace flags fall back to defaults — the caller is
    expected to know what they're resuming and accept current settings.
    """
    return SessionConfig(
        task=checkpoint.task,
        workspace=Path(checkpoint.workspace),
        model=checkpoint.model,
        mode=checkpoint.mode,
        memory_backend="engram",
        memory_repo=Path(checkpoint.memory_repo),
        tool_profile=ToolProfile.FULL,
    )


# ---------------------------------------------------------------------------
# Pause-on-resume bookkeeping (when the resumed loop pauses again)
# ---------------------------------------------------------------------------


def _re_pause(
    components: SessionComponents,
    result,
    checkpoint: Checkpoint,
    resume_state: ResumeState,
    store: SessionStore | None,
) -> int:
    """Handle the case where the resumed loop ALSO pauses.

    Writes a fresh checkpoint over the previous one, re-marks the session
    paused in the store, and prints the new question. ``resume_state.messages``
    has been mutated in place by the inner loop and is the source of truth
    for the conversation snapshot. Returns the CLI exit code (0).
    """
    from harness.checkpoint import (
        CHECKPOINT_FILENAME,
        serialize_checkpoint,
        serialize_memory_state,
        write_checkpoint,
    )

    pause = result.paused
    cp_path = Path(checkpoint.trace_path).parent / CHECKPOINT_FILENAME
    payload = serialize_checkpoint(
        session_id=checkpoint.session_id,
        task=checkpoint.task,
        model=checkpoint.model,
        mode=checkpoint.mode,
        workspace=checkpoint.workspace,
        memory_repo=checkpoint.memory_repo,
        trace_path=checkpoint.trace_path,
        messages=resume_state.messages,
        usage=result.usage,
        loop_state=pause.loop_state,
        memory_state=serialize_memory_state(components.engram_memory),
        pause=pause.pause_info,
        checkpoint_at=datetime.now().isoformat(timespec="seconds"),
    )
    write_checkpoint(cp_path, payload)

    if store is not None:
        store.mark_paused(
            checkpoint.session_id,
            checkpoint_path=str(cp_path),
            paused_at=payload["checkpoint_at"],
        )

    print(f"\n[paused again] {pause.pause_info.question}", file=sys.stderr)
    if pause.pause_info.context:
        print(f"\n  context: {pause.pause_info.context}", file=sys.stderr)
    print(
        f"\nResume with: harness resume {checkpoint.session_id}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def _resume_one(
    *,
    session_id: str,
    reply_arg: str | None,
    db_override: Path | None,
    memory_repo_override: Path | None,
    workspace_override: Path | None,
) -> int:
    db_env = os.getenv("HARNESS_DB_PATH")
    db_path = db_override or (Path(db_env) if db_env else None)
    if db_path is None or not db_path.is_file():
        print(
            "harness resume: SessionStore database required (set HARNESS_DB_PATH or pass --db).",
            file=sys.stderr,
        )
        return 2
    store = SessionStore(db_path)
    record = store.get_session(session_id)
    if record is None:
        store.close()
        print(f"harness resume: no such session: {session_id}", file=sys.stderr)
        return 2
    if record.status != "paused":
        store.close()
        print(
            f"harness resume: session {session_id} has status {record.status!r}, "
            f"not 'paused' — refusing to resume.",
            file=sys.stderr,
        )
        return 2
    if not record.pause_checkpoint:
        store.close()
        print(
            f"harness resume: session {session_id} has no checkpoint path recorded.",
            file=sys.stderr,
        )
        return 2

    cp_path = Path(record.pause_checkpoint)
    try:
        checkpoint = read_checkpoint(cp_path)
    except FileNotFoundError as exc:
        store.close()
        print(f"harness resume: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        store.close()
        print(f"harness resume: invalid checkpoint at {cp_path}: {exc}", file=sys.stderr)
        return 2

    if memory_repo_override is not None:
        checkpoint.memory_repo = str(memory_repo_override)
    if workspace_override is not None:
        checkpoint.workspace = str(workspace_override)

    err = _validate_resume_paths(checkpoint)
    if err is not None:
        store.close()
        print(f"harness resume: {err}", file=sys.stderr)
        return 2

    print(
        f"\n[resume] session {session_id} (paused {record.paused_at})",
        file=sys.stderr,
    )
    print(f"\n  Question: {checkpoint.pause.question}", file=sys.stderr)
    if checkpoint.pause.context:
        print(f"\n  Context:  {checkpoint.pause.context}", file=sys.stderr)
    print("", file=sys.stderr)

    if reply_arg is not None:
        reply = reply_arg.strip()
        if not reply:
            store.close()
            print("harness resume: --reply is empty.", file=sys.stderr)
            return 2
    else:
        try:
            reply = _read_interactive_reply()
        except ValueError as exc:
            store.close()
            print(f"harness resume: {exc}", file=sys.stderr)
            return 2

    # Embed the reply into the conversation. ``messages`` is mutated in
    # place; mismatched tool_use_id raises ValueError, which we surface as a
    # hard error rather than silently dropping the reply.
    try:
        mutate_pause_reply(checkpoint.messages, checkpoint.pause.tool_use_id, reply)
    except ValueError as exc:
        store.close()
        print(f"harness resume: {exc}", file=sys.stderr)
        return 2

    # Build session components with the original session_id and the
    # existing trace path (no truncation — we'll APPEND to the JSONL).
    config = _config_from_checkpoint(checkpoint)
    components = build_session(
        config,
        tools={},
        resume_session_id=checkpoint.session_id,
        resume_trace_path=Path(checkpoint.trace_path),
    )

    if components.engram_memory is None:
        store.close()
        print(
            "harness resume: Engram memory backend required to resume — "
            "checkpoint referenced an Engram session but the build failed.",
            file=sys.stderr,
        )
        return 2

    # Restore the buffered memory events so end_session writes them out
    # alongside the post-resume content.
    restore_memory_state(components.engram_memory, checkpoint.memory_state)

    # Reconstruct loop counters + accumulated usage, then build ResumeState.
    counters = restore_loop_state(checkpoint.loop_state)
    prior_usage = _usage_from_dict(checkpoint.usage)
    resume_state = ResumeState(
        messages=checkpoint.messages,
        counters=counters,
        usage=prior_usage,
    )

    # Flip status back to running so concurrent observers see the right state
    # while the resumed session executes.
    store.mark_resumed(session_id)

    final_text: str | None = None
    turns_used: int | None = None
    max_turns_reached = False
    status = "completed"
    try:
        with components.tracer as tracer:
            result = run(
                checkpoint.task,
                components.mode,
                components.tools,
                components.memory,
                tracer,
                max_turns=config.max_turns,
                max_parallel_tools=config.max_parallel_tools,
                stream_sink=components.stream_sink,
                skip_end_session_commit=True,
                compaction_input_token_threshold=getattr(
                    config, "compaction_input_token_threshold", None
                ),
                pause_handle=components.pause_handle,
                resume_state=resume_state,
            )
    except BaseException:
        status = "error"
        store.close()
        raise

    if result.paused:
        # Re-pause: write a fresh checkpoint and stay in 'paused' state.
        exit_code = _re_pause(components, result, checkpoint, resume_state, store)
        store.close()
        return exit_code

    final_text = result.final_text
    turns_used = result.turns_used
    max_turns_reached = result.max_turns_reached

    # Trace bridge runs over the now-complete trace — same as the fresh-session
    # path in cli.py, just invoked here after resume completes.
    bridge_status = None
    try:
        from harness.trace_bridge import run_trace_bridge

        bridge_result = run_trace_bridge(
            components.trace_path,
            components.engram_memory,
            model=components.config.model,
        )
        bridge_status = ("ok", bridge_result.commit_sha)
        print(
            f"[engram] trace bridge: {len(bridge_result.artifacts)} artifact(s), "
            f"{bridge_result.access_entries} ACCESS entries"
            + (f", commit {bridge_result.commit_sha[:8]}" if bridge_result.commit_sha else ""),
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        bridge_status = ("error", f"{type(exc).__name__}: {exc}")
        print(f"[warning] trace bridge failed: {exc}", file=sys.stderr)

    record_completed_session(
        store,
        session_id=session_id,
        status=status,
        ended_at=datetime.now().isoformat(timespec="seconds"),
        turns_used=turns_used,
        usage=result.usage,
        tool_call_log=[],  # SessionStore captured these during the original run
        final_text=final_text,
        max_turns_reached=max_turns_reached,
        engram_memory=components.engram_memory,
        bridge_status=bridge_status[0] if bridge_status else None,
        bridge_error=bridge_status[1] if bridge_status and bridge_status[0] == "error" else None,
    )
    store.close()

    print("\n" + "=" * 60, file=sys.stderr)
    print(final_text or "")
    print("=" * 60, file=sys.stderr)
    return 0


def _usage_from_dict(raw: dict | None) -> Usage:
    if not raw:
        return Usage.zero()
    fields = {
        f.name: raw.get(f.name, 0) for f in Usage.__dataclass_fields__.values() if f.name in raw
    }
    # ``missing_models`` is a tuple; coerce when the JSON gave a list.
    if "missing_models" in fields and isinstance(fields["missing_models"], list):
        fields["missing_models"] = tuple(fields["missing_models"])
    try:
        return Usage(**fields)
    except TypeError:
        return Usage.zero()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness resume",
        description=(
            "Resume a paused session. Reads the checkpoint, accepts the user's "
            "reply (interactive prompt or --reply), and re-enters the loop."
        ),
    )
    parser.add_argument("session_id", help="The paused session's id (e.g. ses_2026_...).")
    parser.add_argument(
        "--reply",
        default=None,
        help=(
            "Non-interactive reply. When omitted, harness reads multi-line "
            "input from stdin (terminated by EOF)."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite session database. Defaults to $HARNESS_DB_PATH.",
    )
    parser.add_argument(
        "--memory-repo",
        default=None,
        dest="memory_repo",
        help=(
            "Override the Engram repo path recorded in the checkpoint. Use "
            "this only when the original repo has moved on the same machine; "
            "cross-machine relocation is a deliberate follow-up."
        ),
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Override the workspace path recorded in the checkpoint.",
    )
    return parser


def main() -> None:
    """Entry point for ``harness resume``."""
    parser = _build_parser()
    args = parser.parse_args(sys.argv[2:])
    db_override = Path(args.db).expanduser().resolve() if args.db else None
    memory_repo_override = (
        Path(args.memory_repo).expanduser().resolve() if args.memory_repo else None
    )
    workspace_override = Path(args.workspace).expanduser().resolve() if args.workspace else None
    rc = _resume_one(
        session_id=args.session_id,
        reply_arg=args.reply,
        db_override=db_override,
        memory_repo_override=memory_repo_override,
        workspace_override=workspace_override,
    )
    sys.exit(rc)


__all__ = ["main"]
