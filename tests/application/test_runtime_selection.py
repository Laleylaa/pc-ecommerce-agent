import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_factory_rejects_unknown_runtime():
    from app.agent.factory import runtime_type

    with pytest.raises(ValueError, match="Unsupported agent runtime"):
        runtime_type("both")


@pytest.mark.parametrize("selected", ["strands", "legacy"])
def test_make_runtime_is_process_configuration(selected):
    result = subprocess.run(
        ["make", "-n", "backend", f"RUNTIME={selected}", "BACKEND_PORT=8123"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert f"AGENT_RUNTIME={selected} .venv/bin/uvicorn" in result.stdout
    assert "--port 8123" in result.stdout


@pytest.mark.parametrize("selected", ["both", "legacy strands"])
def test_make_rejects_invalid_runtime(selected):
    result = subprocess.run(
        ["make", "-n", "backend", f"RUNTIME={selected}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "RUNTIME must be" in result.stderr


def test_make_without_override_keeps_environment_precedence():
    result = subprocess.run(
        ["make", "-n", "backend"], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert "AGENT_RUNTIME=" not in result.stdout


def test_offline_guard_blocks_io_without_sending():
    import socket

    from sqlalchemy.ext.asyncio import AsyncEngine

    with pytest.raises(AssertionError, match="Offline checks"):
        socket.create_connection(("must-not-resolve.invalid", 443))
    with pytest.raises(AssertionError, match="Offline checks"):
        AsyncEngine.connect(None)



@pytest.mark.parametrize("selected,excluded", [("strands", "legacy"), ("legacy", "loop")])
def test_selected_runtime_imports_independently(selected, excluded):
    code = f'''
import sys
assert 'tests.support.offline' in sys.modules
from app.api.routers import chat
assert not any(n.startswith(('app.agent.loop.', 'app.agent.legacy.')) for n in sys.modules)
from app.agent.factory import runtime_type
runtime_type({selected!r})
assert not any(n.startswith('app.agent.{excluded}.') for n in sys.modules)
if {selected!r} == 'legacy':
    assert not any(n == 'strands' or n.startswith('strands.') for n in sys.modules)
'''
    result = subprocess.run(
        [sys.executable, "-c", code], env=os.environ.copy(), cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
