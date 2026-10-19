"""Explicit opt-in for network-marked tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-network", action="store_true", help="Allow network-marked tests")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-network"):
        return
    excluded = [item for item in items if item.get_closest_marker("network")]
    if excluded:
        config.hook.pytest_deselected(items=excluded)
        items[:] = [item for item in items if item not in excluded]
