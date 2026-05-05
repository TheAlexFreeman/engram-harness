---
trust: medium
source: agent-generated
created: 2026-03-18
type: knowledge
domain: api
tags: [webhooks, callbacks, http]
---

# Webhook delivery

Outbound webhooks are signed with HMAC-SHA256 over the request body
using a per-tenant secret. The signature is sent in the
`X-Webhook-Signature` header.

Delivery uses an exponential backoff with up to seven attempts over
24 hours. After the final failure the event is moved to the dead-letter
queue and a tenant admin is notified.

Webhook payloads are versioned independently of the public API; a
payload version field on the envelope lets consumers handle multiple
shapes during a migration window.
