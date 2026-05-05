# Recall Eval Fixtures

Static corpus + task JSON files used by `harness recall-eval`.

The corpus under `corpus/` is a small, self-contained Engram-shaped repo
spanning four topic clusters (auth, deploy, data, api) plus a couple of
skill files. ACCESS.jsonl encodes synthetic helpfulness history; LINKS.jsonl
encodes co-retrieval edges. Two files (`auth/old-session-model.md` and
`data/old-pipeline-airflow.md`) are superseded — they should be filtered
out by recall unless `include_superseded=True` is passed.

Tasks live as JSON files under `tasks/`. Each task is a dict (or list of
dicts) matching `RecallEvalTask`. Files starting with `_` are skipped.

The fixture is intentionally stable — do not regenerate or rewrite it
unless the eval is being deliberately tuned. Behavioural drift in the
recall path will surface as task failures, which is the regression
signal.
