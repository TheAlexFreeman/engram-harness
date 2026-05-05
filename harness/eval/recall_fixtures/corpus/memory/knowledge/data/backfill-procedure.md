---
trust: medium
source: agent-generated
created: 2026-03-12
type: knowledge
domain: data
tags: [backfill, etl, recovery, snowflake]
---

# Backfill procedure

Backfills run via the `bin/backfill` CLI, which accepts a date range
and a target dataset. The CLI reads the source events from S3,
re-applies the current transformation logic, and writes to a
quarantine schema in the warehouse.

The quarantine output is reviewed by the data team before promotion
to the production schema via a swap. This avoids in-place mutation
of analytics tables that downstream dashboards depend on.

Long-running backfills (> 4 hours) are split into daily chunks to
keep individual runs recoverable.
