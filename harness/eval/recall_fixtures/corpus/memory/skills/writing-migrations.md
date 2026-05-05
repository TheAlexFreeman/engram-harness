---
trust: high
source: user-stated
created: 2026-02-25
type: skill
tags: [migration, database, postgres, alembic]
---

# Writing database migrations

Production migrations follow a strict pattern:

1. Additive only — new columns are nullable or have a default.
2. Backfill is a separate release behind a feature flag.
3. Drops are gated behind a confirmation window after the new schema
   has been live for two weeks with no rollbacks.

Long-running data migrations run in chunks of 10k rows with a small
sleep between chunks to avoid replication lag.

Never modify a previously-applied migration file in place — write a
new one. The migration history is append-only.
