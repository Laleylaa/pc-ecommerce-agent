"""Launch local processes with listener/proxy settings from the root .env."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("backend", "frontend"))
    args = parser.parse_args()
    settings = Settings()
    if args.service == "backend":
        command = [sys.executable, "-m", "uvicorn", "app.main:app", "--reload",
                   "--host", settings.backend_host, "--port", str(settings.backend_port)]
        cwd = ROOT / "backend"
        env = os.environ.copy()
    else:
        command = ["npm", "run", "dev"]
        cwd = ROOT / "frontend"
        host = settings.backend_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        env = {**os.environ, "PC_FRONTEND_PORT": str(settings.frontend_port),
               "PC_DEV_PROXY_TARGET": f"http://{host}:{settings.backend_port}"}
    raise SystemExit(subprocess.call(command, cwd=cwd, env=env))


if __name__ == "__main__":
    main()
