---
trust: high
source: user-stated
created: 2026-02-05
type: knowledge
domain: data
tags: [ingestion, kafka, etl, stream]
---

# Data ingestion pipeline

Raw events arrive on Kafka topic `events.raw`. The ingestion service
consumes the topic, validates each event against the `EventV3` schema,
and writes the validated events to S3 in Parquet format partitioned by
event type and hour.

Schema violations land on `events.dlq` for manual triage. The ingestion
service exposes consumer-lag metrics; an alert fires if lag exceeds
five minutes for more than ten minutes consecutively.

Throughput target: 50k events/sec at p95 < 200 ms end-to-end.
