---
trust: high
source: user-stated
created: 2026-01-30
type: knowledge
domain: api
tags: [rest, conventions, http, design]
---

# REST API conventions

External APIs follow REST conventions:

- Resource paths are plural nouns (`/users`, `/orders`).
- Standard HTTP methods: GET (list/read), POST (create), PATCH
  (partial update), PUT (full replace), DELETE (delete).
- Responses are JSON objects with a top-level `data` field for
  successful results and `error` for failures.
- Pagination uses cursor-based `?cursor=…&limit=…` parameters; offset
  pagination is forbidden because of stability issues at scale.
- Filtering is done via query parameters with the `filter[<field>]`
  syntax.

Versioning is in the URL: `/v1/users`. Breaking changes get a new
major version with a six-month overlap.
