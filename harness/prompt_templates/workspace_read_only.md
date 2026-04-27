## Workspace

You have read-only access to the persistent workspace for orientation and
project context. The workspace contains:

  CURRENT.md  — active threads + freeform notes
  notes/      — persistent working documents
  projects/   — isolated work contexts with goals, questions, and summaries

Available workspace operations use the `work` prefix; the underlying native tool
names are `work_status`, `work_list`, `work_read`, `work_search`,
`work_project_list`, and `work_project_status`.

Tool routing:

  repo/code files           — `read_file`, `list_files`, `grep_workspace`
  active workspace context  — `work_status`, `work_list`, `work_search`, `work_read`
  unknown durable memory    — `memory_context` or `memory_recall`
  known durable memory path — `memory_review`

### work: status

Read CURRENT.md's active threads and freeform notes. Pass `project` to also
include that project's auto-generated SUMMARY.md.

    work: status({})
    work: status({"project": "auth-redesign"})

### work: read

Read any workspace file by relative path.

    work: read({"path": "notes/auth-redesign.md"})

### work: list

List workspace directories by relative path. Use returned paths with
`work: read`; they are relative to `workspace/`, not the repo root.

    work: list({})
    work: list({"path": "projects/auth-redesign/notes"})

### work: search

Keyword search across projects in the workspace. Set `project` to restrict to a
single project. Read returned paths with `work: read`.

    work: search({"query": "token refresh"})

### Projects

List projects or read a project's status summary.

    work: project.list({})
    work: project.status({"name": "auth-redesign"})

This session is read-only: do not attempt to create, update, archive, advance
plans, write scratch, or promote workspace content.
