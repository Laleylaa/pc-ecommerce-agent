"""Opt-in pytest guard for the isolated orchestration review.

Use ``python -m pytest -p scripts.orchestration_audit_safety ...`` from backend.
This is a guard for the inspected tests, not a sandbox for arbitrary test code.
"""

import socket

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Audit forbids network and configured database access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(AsyncEngine, "connect", denied)
