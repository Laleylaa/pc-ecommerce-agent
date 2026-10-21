"""Guard collection and execution, including direct pytest invocations."""
from tests.support import offline  # noqa: F401


def pytest_collection_modifyitems(config, items):
    forbidden = [item for item in items if item.get_closest_marker("network")]
    if forbidden:
        import pytest

        raise pytest.UsageError("Network tests are forbidden in the isolated test suite")
