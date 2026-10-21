"""Inherited guard for Python subprocesses launched by the offline runner."""
import os
import sys

if os.environ.get("PC_AGENT_OFFLINE_TEST") == "1":
    try:
        from tests.support import offline  # noqa: F401
    except BaseException:
        # Python otherwise only logs sitecustomize errors and continues unguarded.
        sys.stderr.write("Offline subprocess guard could not initialize\n")
        os._exit(2)
