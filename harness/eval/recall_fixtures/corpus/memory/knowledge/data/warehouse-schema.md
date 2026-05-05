---
trust: high
source: user-stated
created: 2026-02-15
type: knowledge
domain: data
tags: [warehouse, snowflake, schema, dimensional]
---

# Warehouse schema

The analytics warehouse runs on Snowflake. The schema follows a
classic star design: `fct_*` fact tables (events, sessions, orders) at
the center, surrounded by `dim_*` dimension tables (users, products,
geography).

Fact tables retain raw event grain for 13 months; older data is
aggregated into the `agg_*` rollup tables and the raw rows are dropped.

dbt orchestrates the transformations. Each model has a `data_test`
asserting non-null primary keys, accepted-value constraints on enum
columns, and referential integrity to the dimension tables.
