---
trust: medium
source: agent-generated
created: 2026-02-20
type: knowledge
domain: deploy
tags: [ci, cd, pipeline, github-actions]
---

# CI/CD pipeline

GitHub Actions runs the pipeline on every push to a feature branch.
Stages: lint, unit tests, integration tests, container build, push to
the registry, and (on `main` only) staging deploy.

The integration test stage spins up an ephemeral Postgres and Redis
via docker-compose. Test data lives under `tests/fixtures/`.

Deploys to production are not automated — they require manual approval
from a release manager via the GitHub deployment review.
