---
trust: high
source: user-stated
created: 2026-02-08
type: knowledge
domain: api
tags: [errors, http, responses]
---

# API error response shape

API errors return a JSON object with the shape:

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Human-readable explanation.",
    "details": [{"field": "email", "issue": "format"}],
    "request_id": "req_abc123"
  }
}
```

`code` is a stable, machine-parseable identifier. `message` is a human
description suitable for displaying in the UI. `details` is an
optional array of per-field problems for validation errors.
`request_id` is always present and matches the value in the
`X-Request-Id` response header for log correlation.

HTTP status codes follow standard semantics: 400 (validation), 401
(unauthenticated), 403 (forbidden), 404 (not found), 409 (conflict),
422 (semantic validation), 429 (rate limited), 5xx (server error).
