---
trust: high
source: user-stated
created: 2026-01-22
type: knowledge
domain: deploy
tags: [kubernetes, rollout, deployment, helm]
---

# Kubernetes rollout strategy

Production deploys use Helm charts with the rolling-update strategy:
`maxSurge: 25%` and `maxUnavailable: 0`. Readiness probes hit
`/healthz/ready` with a 5-second initial delay and a 1-second period.

Rollouts are gated behind a manual `helm upgrade --atomic` invocation
from the release runner. Failures auto-rollback to the previous
ReplicaSet within five minutes.

Image tags are immutable git SHAs. The `latest` tag is forbidden in
production manifests.
