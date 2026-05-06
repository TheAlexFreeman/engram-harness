"""Smoke test for ``harness drift`` (P1.5).

Mirrors the CI smoke step at ``.github/workflows/ci.yml``. Shipping it as
a unit test too means a developer running the pytest suite locally
notices any regression in cmd_drift / analytics before they push.
"""

from __future__ import annotations

from harness.tests.fixtures.drift import main


def test_drift_smoke_clean_fixture_exits_zero() -> None:
    assert main() == 0
