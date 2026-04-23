from __future__ import annotations

import json
from typing import Any

from harness.tools import Tool
from harness.tools.fs.scope import normalize_workspace_relative

_PATH_KEY_ALLOWLIST: frozenset[str] = frozenset({"path", "from_path", "to_path", "root"})


def parse_tool_arguments(raw: str | object) -> dict[str, Any]:
    """Parse function tool ``arguments`` JSON; empty dict on failure."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _description_suggests_workspace_path(desc: str) -> bool:
    d = desc.lower()
    return "relative path" in d or "workspace" in d


def _keys_to_normalize_from_schema(tool: Tool) -> frozenset[str]:
    props = tool.input_schema.get("properties") or {}
    if not isinstance(props, dict):
        return frozenset()
    keys: set[str] = set()
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("type") != "string":
            continue
        desc = str(spec.get("description") or "")
        if key in _PATH_KEY_ALLOWLIST or _description_suggests_workspace_path(desc):
            keys.add(key)
    return frozenset(keys)


def canonicalize_tool_args(
    name: str,
    args: dict[str, Any],
    tool: Tool | None,
) -> dict[str, Any]:
    """Return a shallow copy of ``args`` with path-like string fields normalized."""
    if not args:
        return dict(args) if args is not None else {}

    if tool is not None:
        keys = _keys_to_normalize_from_schema(tool)
    else:
        keys = _PATH_KEY_ALLOWLIST

    out = dict(args)
    for k in keys:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = normalize_workspace_relative(v)
    return out


def arguments_json_canonical(
    raw: str | object,
    name: str,
    tool: Tool | None,
) -> str:
    """Parse tool arguments, canonicalize path-like fields, return compact JSON string."""
    args = parse_tool_arguments(raw)
    canon = canonicalize_tool_args(name, args, tool)
    return json.dumps(canon, separators=(",", ":"), ensure_ascii=True, default=str)


def maybe_canonicalize_function_call_item(
    item: dict[str, Any],
    tools: dict[str, Tool] | None,
) -> dict[str, Any]:
    """If ``item`` is a harness function_call, rewrite ``arguments`` in place; return item."""
    if tools is None:
        return item
    if item.get("type") != "function_call":
        return item
    fn_name = item.get("name")
    if not isinstance(fn_name, str) or fn_name not in tools:
        return item
    raw = item.get("arguments", "{}")
    item["arguments"] = arguments_json_canonical(raw, fn_name, tools[fn_name])
    return item
