import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_default_runtime_is_loop_and_legacy_remains_explicit(monkeypatch):
    from app.agent.factory import runtime_type
    from app.core.config import Settings

    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    default = Settings(_env_file=None)
    assert default.agent_runtime == "strands"
    assert runtime_type(default.agent_runtime).__name__ == "AgentLoopRuntime"
    monkeypatch.setenv("AGENT_RUNTIME", "legacy")
    assert Settings(_env_file=None).agent_runtime == "legacy"


def test_factory_rejects_unknown_runtime():
    from app.agent.factory import runtime_type

    with pytest.raises(ValueError, match="Unsupported agent runtime"):
        runtime_type("both")


@pytest.mark.parametrize("selected", ["strands", "legacy"])
def test_make_runtime_is_process_configuration(selected, tmp_path):
    probe = tmp_path / "probe.mk"
    probe.write_text("probe:\n\t@printf '%s\\n' \"$$AGENT_RUNTIME\" \"$$BACKEND_PORT\"\n")
    result = subprocess.run(
        ["make", "--no-print-directory", "-f", "Makefile", "-f", str(probe), "probe",
         f"RUNTIME={selected}", "BACKEND_PORT=8123"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert result.stdout.splitlines() == [selected, "8123"]


@pytest.mark.parametrize("selected", ["both", "legacy strands"])
def test_make_rejects_invalid_runtime(selected):
    result = subprocess.run(
        ["make", "-n", "backend", f"RUNTIME={selected}"],
        cwd=ROOT, capture_output=True, text=True, check=False,
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
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
