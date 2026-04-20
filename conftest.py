"""Phase-0 conftest: shim stdlib tomllib (py3.11+) onto tomli for py3.10 sandboxes."""
import sys
try:
    import tomllib  # noqa: F401  Python 3.11+
except ImportError:  # pragma: no cover
    import tomli
    sys.modules.setdefault('tomllib', tomli)
