---
trust: medium
source: agent-generated
created: 2026-02-12
type: knowledge
domain: deploy
tags: [config, environment, secrets, vault]
---

# Environment configuration

Each deployment environment (dev, staging, prod) gets its own Helm
values file at `deploy/values/<env>.yaml`. Non-secret configuration
values live in the values file directly; secrets are referenced by
Vault path and injected at pod start by the Vault Agent sidecar.

Environment variables follow the prefix convention `APP_<SECTION>_<KEY>`
(e.g. `APP_DB_HOST`, `APP_AUTH_SIGNING_KEY_PATH`). The application
loader fails fast on any unset required key — no implicit defaults in
production.
