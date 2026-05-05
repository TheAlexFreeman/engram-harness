---
trust: low
source: agent-generated
created: 2025-08-01
valid_from: 2025-08-01
valid_to: 2026-01-15
superseded_by: session-tokens.md
type: knowledge
domain: auth
tags: [session, tokens, deprecated]
---

# Old session model (DEPRECATED)

This file documents the old opaque-token session model that was replaced
by signed JWTs in session-tokens.md.

The old design stored opaque random tokens in Redis with a 30-minute TTL.
Validation hit Redis on every request.

Do not implement against this — it is preserved only for migration
context.
