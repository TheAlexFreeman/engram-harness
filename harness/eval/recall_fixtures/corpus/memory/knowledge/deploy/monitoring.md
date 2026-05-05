---
trust: medium
source: agent-generated
created: 2026-03-04
type: knowledge
domain: deploy
tags: [monitoring, prometheus, grafana, alerts]
---

# Monitoring and alerting

Metrics are scraped by Prometheus at the `/metrics` endpoint; dashboards
live in Grafana. Standard SLO panels per service: request rate, error
rate, p95 latency, and saturation.

Alerting rules live in `deploy/prometheus/alerts/`. Critical alerts
page the on-call rotation; warning alerts go to the team Slack
channel. The on-call runbook for each alert is linked from the alert
description.

Logs are shipped via the OpenTelemetry collector to the central
log store with a 14-day retention.
