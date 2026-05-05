---
trust: low
source: agent-generated
created: 2025-09-10
valid_from: 2025-09-10
valid_to: 2026-02-05
superseded_by: ingestion-pipeline.md
type: knowledge
domain: data
tags: [airflow, deprecated, pipeline]
---

# Airflow ingestion pipeline (DEPRECATED)

Documents the legacy Airflow-based hourly batch ingestion that has
been replaced by the streaming Kafka design in ingestion-pipeline.md.

Hourly DAG read events from S3, applied transformations in Python
operators, and wrote to the warehouse. The minimum end-to-end latency
was about 70 minutes — unacceptable once near-real-time analytics
became a product requirement.

Preserved for migration context only.
