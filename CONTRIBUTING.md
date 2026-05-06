# Contributing

## Setup

```bash
pip install -e ".[dev]"
```

Use extras when touching optional surfaces:

```bash
pip install -e ".[dev,api]"     # FastAPI server tests
pip install -e ".[dev,search]"  # semantic recall path
```

## Local Checks

```bash
python -m ruff check harness conftest.py
python -m ruff format --check harness conftest.py
python -m pytest harness/tests/ -v
harness recall-eval --really-run
```

Integration tests are opt-in:

```bash
python -m pytest harness/tests/ --integration -v
```

## Useful Smoke Commands

```bash
harness status
harness recall-eval
harness optimize
harness decay-sweep --memory-repo ./engram
```

`harness eval --really-run` and model-backed consolidation spend API tokens;
use dry-run modes unless you are intentionally measuring live model behavior.

## Planning Docs

`ROADMAP.md` is the long-term architecture plan. `docs/improvement-plans-2026.md`
tracks shipped and remaining improvement themes. `docs/architecture.md` is the
short contributor map for the current code layout.
