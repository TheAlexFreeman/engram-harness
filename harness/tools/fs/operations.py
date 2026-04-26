from __future__ import annotations

import shutil
from datetime import datetime, timezone

from .scope import (
    MAX_GLOB_RESULTS,
    MAX_LIST_ENTRIES,
    MAX_READ_CHARS,
    WorkspaceScope,
    path_is_within_boundary,
    truncate_text,
)


class ReadFile:
    name = "read_file"
    description = (
        "Read a text file at a path relative to the workspace (UTF-8 by default). "
        "When Engram memory is mounted, also accepts memory aliases like "
        "'memory:/knowledge/foo.md' or bare roots like 'knowledge/foo.md'. "
        "Supports optional character slicing (offset/limit), optional 1-based inclusive "
        "line range (line_start/line_end), and max_chars on the returned slice. "
        "Output is capped at a large internal limit with a truncation suffix. "
        "Do not use on directories."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path within workspace, or mounted memory alias like "
                    "'memory:/knowledge/foo.md' / 'knowledge/foo.md'."
                ),
            },
            "encoding": {
                "type": "string",
                "description": "Text encoding. Default utf-8.",
            },
            "errors": {
                "type": "string",
                "enum": ["strict", "replace"],
                "description": "How to handle undecodable bytes. Default strict.",
            },
            "offset": {
                "type": "integer",
                "description": "0-based character offset into the file (after full decode). Ignored if line_start/line_end set.",
            },
            "limit": {
                "type": "integer",
                "description": "Max characters to return after offset. Ignored if line_start/line_end set.",
            },
            "line_start": {
                "type": "integer",
                "description": "1-based start line (inclusive). Use with line_end for a line slice.",
            },
            "line_end": {
                "type": "integer",
                "description": "1-based end line (inclusive). Defaults to line_start if only line_start is set.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Hard cap on returned string length after slicing (in addition to internal limits).",
            },
        },
        "required": ["path"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args["path"])
        if not path.is_file():
            raise ValueError(f"not a file: {args['path']!r}")
        encoding = args.get("encoding") or "utf-8"
        errors = args.get("errors") or "strict"
        if errors not in ("strict", "replace"):
            raise ValueError("errors must be strict or replace")

        raw = path.read_text(encoding=encoding, errors=errors)
        line_start = args.get("line_start")
        line_end = args.get("line_end")

        if line_start is not None or line_end is not None:
            ls = int(line_start) if line_start is not None else 1
            le = int(line_end) if line_end is not None else ls
            if ls < 1:
                raise ValueError("line_start must be >= 1")
            if le < ls:
                raise ValueError("line_end must be >= line_start")
            lines = raw.splitlines(keepends=True)
            if ls > len(lines):
                out = ""
            else:
                out = "".join(lines[ls - 1 : le])
        else:
            offset = int(args.get("offset") or 0)
            if offset < 0:
                raise ValueError("offset must be non-negative")
            limit = args.get("limit")
            if limit is not None:
                end = offset + int(limit)
                out = raw[offset:end]
            else:
                out = raw[offset:]

        cap = MAX_READ_CHARS
        if args.get("max_chars") is not None:
            cap = min(cap, int(args["max_chars"]))

        out, truncated = truncate_text(out, cap)
        if truncated and line_start is None and line_end is None:
            out += f"\n(full decoded length {len(raw)} chars)"
        return out


class ListFiles:
    name = "list_files"
    description = (
        "List files and directories at a path relative to the workspace (non-recursive). "
        "When Engram memory is mounted, accepts memory aliases like 'memory:/knowledge' "
        "or bare roots like 'knowledge'. "
        "Directories are suffixed with a trailing slash. Defaults to workspace root. "
        "Large directories are truncated with a notice."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path. Defaults to '.'. Mounted memory aliases are accepted "
                    "when available."
                ),
            }
        },
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args.get("path", "."))
        if not path.is_dir():
            raise ValueError(f"not a directory: {args.get('path', '.')!r}")
        entries = []
        for item in sorted(path.iterdir()):
            entries.append(item.name + ("/" if item.is_dir() else ""))
        if not entries:
            return "(empty directory)"
        if len(entries) > MAX_LIST_ENTRIES:
            head = entries[:MAX_LIST_ENTRIES]
            extra = len(entries) - MAX_LIST_ENTRIES
            return (
                "\n".join(head)
                + f"\n\n[harness: {extra} more entries omitted; refine path or use glob_files]"
            )
        return "\n".join(entries)


class PathStat:
    name = "path_stat"
    description = (
        "Return metadata for a path under the workspace or mounted memory root: size, "
        "type flags, mtime (UTC ISO), and permission bits (octal, platform-specific). "
        "Relative paths only."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path within workspace, or mounted memory alias like "
                    "'memory:/knowledge/foo.md' / 'knowledge/foo.md'."
                ),
            }
        },
        "required": ["path"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args["path"])
        st = path.stat()
        mode_oct = oct(st.st_mode & 0o777)
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        rel = self.scope.describe_path(path)
        lines = [
            f"path: {rel}",
            f"exists: {path.exists()}",
            f"size: {st.st_size}",
            f"is_file: {path.is_file()}",
            f"is_dir: {path.is_dir()}",
            f"is_symlink: {path.is_symlink()}",
            f"mtime_utc: {mtime}",
            f"mode: {mode_oct}",
        ]
        return "\n".join(lines)


class GlobFiles:
    name = "glob_files"
    description = (
        "List files matching a glob pattern under a workspace-relative directory. "
        "When Engram memory is mounted, root may be a memory alias and patterns that "
        "start with a bare memory root (e.g. knowledge/**/*.md) can find memory files. "
        "Use ** for recursive patterns (e.g. **/*.py). Results are sorted; "
        "symlink targets outside the active boundary are skipped. Capped by max_results."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob relative to root, e.g. '*.md', 'src/**/*.py', or "
                    "'knowledge/**/*.md' when memory is mounted."
                ),
            },
            "root": {
                "type": "string",
                "description": (
                    "Directory under workspace or mounted memory to start from. Default '.'."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum paths to return. Default 5000.",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        pattern = args["pattern"]
        root_arg = args.get("root", ".")
        entry = self.scope.resolve_entry(root_arg)
        root = entry.path
        if not root.is_dir():
            raise ValueError("root must be a directory")
        max_results = int(args.get("max_results") or MAX_GLOB_RESULTS)
        max_results = min(max(max_results, 1), MAX_GLOB_RESULTS)

        hits = self._collect_hits(root, pattern, entry.boundary, max_results)
        if (
            not hits
            and root_arg == "."
            and self.scope.memory_root is not None
            and self.scope._is_bare_memory_path(pattern)
        ):
            memory_root = self.scope.memory_root.resolve()
            hits = self._collect_hits(memory_root, pattern, memory_root, max_results)

        if not hits:
            return "(no matches)"
        msg = "\n".join(hits)
        if len(hits) >= max_results:
            msg += f"\n\n[harness: capped at max_results={max_results}]"
        return msg

    def _collect_hits(
        self,
        root,
        pattern: str,
        boundary,
        max_results: int,
    ) -> list[str]:
        hits: list[str] = []
        for p in sorted(root.glob(pattern)):
            try:
                pr = p.resolve()
            except OSError:
                continue
            if not path_is_within_boundary(pr, boundary):
                continue
            hits.append(self.scope.describe_path(pr))
            if len(hits) >= max_results:
                break
        return hits


class Mkdir:
    name = "mkdir"
    description = (
        "Create a directory at a path relative to the workspace. "
        "Creates parent directories as needed. Succeeds if the directory already exists."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative directory path within workspace."}
        },
        "required": ["path"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args["path"])
        path.mkdir(parents=True, exist_ok=True)
        return f"mkdir ok: {args['path']}"


class EditFile:
    name = "edit_file"
    description = (
        "Edit a text file by replacing `old_str` with `new_str` (UTF-8). "
        "If the file does not exist AND `old_str` is empty, the file is created with `new_str`. "
        "`old_str` must match exactly and appear exactly once. "
        "`old_str` and `new_str` must differ."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_str": {
                "type": "string",
                "description": "Exact text to replace. Empty = create file.",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text.",
            },
        },
        "required": ["path", "old_str", "new_str"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args["path"])
        old, new = args["old_str"], args["new_str"]
        if old == new:
            raise ValueError("old_str and new_str must differ")

        if not path.exists():
            if old != "":
                raise FileNotFoundError(f"{path.name} does not exist; pass empty old_str to create")
            path.write_text(new, encoding="utf-8")
            return f"created {path.name}"

        content = path.read_text(encoding="utf-8")
        occurrences = content.count(old)
        if occurrences == 0:
            raise ValueError(f"old_str not found in {path.name}")
        if occurrences > 1:
            raise ValueError(f"old_str appears {occurrences} times in {path.name}; must be unique")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return f"edited {path.name}"


class WriteFile:
    name = "write_file"
    description = (
        "Write full file contents (UTF-8). Use for intentional whole-file replace or new files; "
        "prefer edit_file for small surgical edits. Set create_only to forbid overwriting an "
        "existing file; set must_exist to forbid creating a new file."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path."},
            "content": {"type": "string", "description": "Full new file contents."},
            "create_only": {
                "type": "boolean",
                "description": "If true, error if the file already exists. Default false.",
            },
            "must_exist": {
                "type": "boolean",
                "description": "If true, error if the file does not exist yet. Default false.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args["path"])
        content = args["content"]
        create_only = bool(args.get("create_only"))
        must_exist = bool(args.get("must_exist"))
        exists = path.exists()
        if create_only and exists:
            raise FileExistsError(f"{args['path']!r} already exists (create_only)")
        if must_exist and not exists:
            raise FileNotFoundError(f"{args['path']!r} does not exist (must_exist)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"write_file ok: {args['path']}"


class AppendFile:
    name = "append_file"
    description = (
        "Append UTF-8 text to a file. Use for long generated documents that "
        "should be written in chunks instead of one huge write_file call. "
        "Set create to false to require the file to already exist."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path."},
            "content": {"type": "string", "description": "Text to append."},
            "create": {
                "type": "boolean",
                "description": "If false, error if the file does not exist. Default true.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        path = self.scope.resolve(args["path"])
        content = args["content"]
        create = bool(args.get("create", True))
        if not create and not path.exists():
            raise FileNotFoundError(f"{args['path']!r} does not exist")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"append_file ok: {args['path']} ({len(content)} chars)"


class DeletePath:
    name = "delete_path"
    description = (
        "Delete a file or directory under the workspace. confirm must be true. "
        "For non-empty directories set recursive true (uses rm -rf semantics)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {
                "type": "boolean",
                "description": "If true, delete directories and their contents. Default false.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to perform the delete.",
            },
        },
        "required": ["path", "confirm"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        if not args.get("confirm"):
            raise ValueError("confirm must be true to delete_path")
        path = self.scope.resolve(args["path"])
        recursive = bool(args.get("recursive"))
        if not path.exists():
            raise FileNotFoundError(args["path"])
        if path.is_dir():
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink()
        return f"delete_path ok: {args['path']}"


class MovePath:
    name = "move_path"
    description = (
        "Move or rename a path within the workspace. confirm must be true. "
        "Parent directories of the destination are created as needed."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "from_path": {"type": "string"},
            "to_path": {"type": "string"},
            "confirm": {
                "type": "boolean",
                "description": "Must be true to perform the move.",
            },
        },
        "required": ["from_path", "to_path", "confirm"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        if not args.get("confirm"):
            raise ValueError("confirm must be true to move_path")
        src = self.scope.resolve(args["from_path"])
        dst = self.scope.resolve(args["to_path"])
        if not src.exists():
            raise FileNotFoundError(args["from_path"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"move_path ok: {args['from_path']} -> {args['to_path']}"


class CopyPath:
    name = "copy_path"
    description = (
        "Copy a file or directory under the workspace. For directories, set recursive true "
        "(merges into existing destination directories when supported)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "from_path": {"type": "string"},
            "to_path": {"type": "string"},
            "recursive": {
                "type": "boolean",
                "description": "Required true when copying a directory. Ignored for files.",
            },
        },
        "required": ["from_path", "to_path"],
    }

    def __init__(self, scope: WorkspaceScope):
        self.scope = scope

    def run(self, args: dict) -> str:
        src = self.scope.resolve(args["from_path"])
        dst = self.scope.resolve(args["to_path"])
        recursive = bool(args.get("recursive"))
        if not src.exists():
            raise FileNotFoundError(args["from_path"])
        if src.is_dir():
            if not recursive:
                raise ValueError("copy_path on a directory requires recursive: true")
            if dst.exists() and not dst.is_dir():
                raise ValueError("destination exists and is not a directory")
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return f"copy_path ok: {args['from_path']} -> {args['to_path']}"
