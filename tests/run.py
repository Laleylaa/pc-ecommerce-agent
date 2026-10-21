"""Reviewed offline-only pytest entry; no dependency installation or service probes."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
paths = [str(ROOT), str(ROOT / "backend"), str(ROOT / "tests")]
sys.path[:0] = paths
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "tests/support/bootstrap"), *paths])
os.environ["PC_AGENT_OFFLINE_TEST"] = "1"
os.environ["NODE_OPTIONS"] = "--require=" + str(ROOT / "tests/support/node_offline.cjs")
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

import pytest  # noqa: E402

from tests.support import offline  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(pytest.main([
        "-c", str(ROOT / "pytest.ini"), "-p", "pytest_asyncio.plugin", *sys.argv[1:],
    ]))
