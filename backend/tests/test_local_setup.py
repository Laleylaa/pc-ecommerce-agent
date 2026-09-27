"""Exercise infrastructure setup without touching real Podman resources."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def infra_stub(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(PROJECT_ROOT / "scripts/podman-infra.sh", scripts / "podman-infra.sh")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands.jsonl"
    stub = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\\n")
if name == "podman" and args[:2] == ["container", "exists"]:
    sys.exit(0 if os.environ.get("STUB_EXISTING") else 1)
if name == "podman" and args[:1] == ["port"]:
    print("0.0.0.0:9999")
if name == "curl":
    sys.exit(1 if os.environ.get("STUB_CHROMA_FAIL") or "/v2/" in args[-1] else 0)
'''
    for name in ("podman", "curl", "sleep"):
        path = binaries / name
        path.write_text(stub)
        path.chmod(0o755)
    (tmp_path / ".env").write_text(
        'PODMAN_PROJECT_NAME="fixture-agent"\nPOSTGRES_PORT=15432\n'
        'REDIS_PORT=16379\nCHROMA_PORT=18001\nPOSTGRES_PASSWORD="literal$(false)"\n'
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("POSTGRES_", "REDIS_", "CHROMA_", "PODMAN_", "CONFIRM_RESET"))
    }
    env.update(PATH=str(binaries) + os.pathsep + env["PATH"], STUB_LOG=str(log))

    def run(command="up", **overrides):
        result = subprocess.run(
            ["bash", str(scripts / "podman-infra.sh"), command],
            cwd=tmp_path,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=20,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return run


def test_infra_reads_dotenv_and_environment_and_waits_for_chroma(infra_stub):
    result, calls = infra_stub(CHROMA_PORT="28001")
    assert result.returncode == 0, result.stderr
    containers = [call for call in calls if call[:2] == ["podman", "run"]]
    assert len(containers) == 3
    assert "fixture-agent-postgres" in containers[0]
    assert "15432:5432" in containers[0]
    assert "POSTGRES_PASSWORD=literal$(false)" in containers[0]
    assert "16379:6379" in containers[1]
    assert "28001:8000" in containers[2]
    assert "latest" not in containers[2][-1]
    urls = [call[-1] for call in calls if call[0] == "curl"]
    assert urls == [
        "http://127.0.0.1:28001/api/v2/heartbeat",
        "http://127.0.0.1:28001/api/v1/heartbeat",
    ]
    assert calls[-1][:2] == ["podman", "ps"]


def test_infra_fails_if_chroma_never_becomes_ready(infra_stub):
    result, calls = infra_stub(STUB_CHROMA_FAIL="1")
    assert result.returncode != 0
    assert "chroma did not become ready" in result.stderr
    assert not any(call[:2] == ["podman", "ps"] for call in calls)


def test_existing_container_port_mismatch_does_not_replace_resources(infra_stub):
    result, calls = infra_stub(STUB_EXISTING="1")
    assert result.returncode != 0
    assert "expected host port 15432" in result.stderr
    assert not any(call[1] in {"run", "rm", "stop", "start"} for call in calls)


def test_reset_requires_explicit_confirmation_before_any_podman_call(infra_stub):
    result, calls = infra_stub("reset")
    assert result.returncode != 0
    assert "CONFIRM_RESET=1" in result.stderr
    assert calls == []
