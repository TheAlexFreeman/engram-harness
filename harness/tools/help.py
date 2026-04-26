from __future__ import annotations


class ToolHelp:
    name = "tool_help"
    mutates = False
    description = (
        "Return a compact routing guide for choosing between repo filesystem, "
        "internal workspace, and Engram memory tools."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": (
                    "Optional focus area: workspace, memory, files, or all. "
                    "Defaults to all."
                ),
            }
        },
    }

    def run(self, args: dict) -> str:
        topic = (args.get("topic") or "all").strip().lower()
        sections: list[str] = []

        if topic in {"all", "files", "repo", "code"}:
            sections.append(
                "Repo/code files:\n"
                "- list directories with `list_files`\n"
                "- search file contents with `grep_workspace`\n"
                "- read repo-root-relative files with `read_file`"
            )

        if topic in {"all", "workspace", "work"}:
            sections.append(
                "Internal workspace:\n"
                "- orient with `work_status`\n"
                "- list workspace directories with `work_list`\n"
                "- search project notes with `work_search`\n"
                "- read returned workspace paths with `work_read`, not `read_file`"
            )

        if topic in {"all", "memory", "engram"}:
            sections.append(
                "Engram memory:\n"
                "- load broad context with `memory_context`\n"
                "- search unknown topics with `memory_recall`\n"
                "- read known memory paths with `memory_review`\n"
                "- preserve durable observations with `memory_remember` when available"
            )

        if not sections:
            return "(unknown help topic; use workspace, memory, files, or all)\n"
        return "\n\n".join(sections) + "\n"
