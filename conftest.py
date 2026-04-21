"""Phase-0 conftest: shim stdlib tomllib (py3.11+) onto tomli for py3.10 sandboxes."""
import sys
try:
    import tomllib  # noqa: F401  Python 3.11+
except ImportError:  # pragma: no cover
    import tomli
    sys.modules.setdefault('tomllib', tomli)


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires filesystem, git, and optional API deps).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (deselected by default; use --integration).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip_integration = __import__("pytest").mark.skip(reason="use --integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
