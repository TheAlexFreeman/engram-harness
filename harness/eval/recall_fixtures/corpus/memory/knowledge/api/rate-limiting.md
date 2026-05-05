---
trust: medium
source: agent-generated
created: 2026-02-22
type: knowledge
domain: api
tags: [rate-limit, throttling, redis]
---

# API rate limiting

Rate limits are enforced at the API gateway. The implementation uses a
token-bucket algorithm with per-tenant Redis keys.

Default tier limits: 100 requests/sec sustained, 200 requests/sec burst.
Authenticated users above the free tier get configurable limits set
in their tenant record.

When a request is rate-limited the gateway returns HTTP 429 with a
`Retry-After` header indicating the seconds until the next token is
available.

Rate-limit decisions are logged at the trace level so support can
diagnose customer reports of throttling.
