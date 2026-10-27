"""Exercise infrastructure setup without touching real Podman resources."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def infra_stub(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(PROJECT_ROOT / "scripts/podman-infra.sh", scripts / "podman-infra.sh")
    shutil.copy2(PROJECT_ROOT / "scripts/local-config.sh", scripts / "local-config.sh")
    # Copy only the pure configuration code. Never load the developer's .env.
    for relative in ("app/__init__.py", "app/core/__init__.py", "scripts/__init__.py"):
        target = tmp_path / "backend" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    for relative in ("app/core/config.py", "scripts/local_config.py"):
        shutil.copy2(PROJECT_ROOT / "backend" / relative, tmp_path / "backend" / relative)
    python = tmp_path / "backend/.venv/bin/python"
    python.parent.mkdir(parents=True)
    guarded_path = os.pathsep.join([
        str(PROJECT_ROOT / "tests/support/bootstrap"), str(tmp_path / "backend"),
        str(PROJECT_ROOT),
    ])
    python.write_text(
        "#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(guarded_path) + "\nexec "
        + shlex.quote(sys.executable) + ' "$@"\n'
    )
    python.chmod(0o755)
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
        path.write_text(stub.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        path.chmod(0o755)
    (tmp_path / ".env").write_text(
        'PODMAN_PROJECT_NAME="fixture-agent"\nPOSTGRES_PORT=15432\n'
        'REDIS_PORT=16379\nCHROMA_PORT=18001\nPOSTGRES_PASSWORD="literal$(false)"\n'
        'REDIS_ENABLED=true\nCHROMA_ENABLED=true\n'
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
            timeout=60,
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


def test_minimal_infrastructure_does_not_start_optional_services(infra_stub):
    result, calls = infra_stub(REDIS_ENABLED="false", CHROMA_ENABLED="false")
    assert result.returncode == 0, result.stderr
    containers = [call for call in calls if call[:2] == ["podman", "run"]]
    assert len(containers) == 1
    assert "fixture-agent-postgres" in containers[0]
    assert not any(call[0] == "curl" for call in calls)


def test_invalid_configuration_stops_before_podman(infra_stub):
    result, calls = infra_stub(POSTGRES_PORT="invalid-port")
    assert result.returncode != 0
    assert "postgres_port" in result.stderr
    assert calls == []


def test_public_makefile_can_plan_setup_without_development_files(tmp_path):
    shutil.copy2(PROJECT_ROOT / "Makefile", tmp_path / "Makefile")
    result = subprocess.run(
        ["make", "-n", "setup-local"], cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert "scripts.demo.load" in result.stdout
    assert "--extra dev" not in result.stdout
    assert all(name not in result.stdout for name in (
        "tests/", "evals/", "seed_user_isolation", "seed_demo", "sync_knowledge",
    ))


def test_make_preserves_dotenv_defaults_and_explicit_port_overrides(tmp_path):
    shutil.copy2(PROJECT_ROOT / "Makefile", tmp_path / "Makefile")
    probe = tmp_path / "probe.py"
    probe.write_text('import os; print(repr(os.environ.get("BACKEND_PORT")))\n')
    recipe = "\nprobe:\n\t@" + shlex.quote(sys.executable) + " " + shlex.quote(str(probe)) + "\n"
    with (tmp_path / "Makefile").open("a") as stream:
        stream.write(recipe)
    env = {k: v for k, v in os.environ.items()
           if k not in {"BACKEND_HOST", "BACKEND_PORT", "FRONTEND_PORT", "MAKEFLAGS"}}
    for args, expected in (([], "None"), (["BACKEND_PORT=18000"], "'18000'")):
        result = subprocess.run(
            ["make", "--no-print-directory", "probe", *args],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == expected


def test_launcher_uses_root_ports_without_exposing_settings_to_frontend(monkeypatch):
    from app.core.config import Settings

    from scripts import run_local

    settings = Settings(_env_file=None, frontend_port=15173, backend_port=18000,
                        llm_api_key="synthetic-secret")
    monkeypatch.setattr(run_local, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["run_local", "frontend"])
    calls = []

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        return 0

    monkeypatch.setattr(run_local.subprocess, "call", launch)
    with pytest.raises(SystemExit) as exc:
        run_local.main()
    assert exc.value.code == 0
    command, kwargs = calls[0]
    assert command == ["npm", "run", "dev"]
    assert kwargs["env"]["PC_DEV_PROXY_TARGET"] == "http://127.0.0.1:18000"
    assert kwargs["env"]["PC_FRONTEND_PORT"] == "15173"
    assert "synthetic-secret" not in kwargs["env"].values()
