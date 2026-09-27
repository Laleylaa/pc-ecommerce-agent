#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

failed=0
for tool in uv node npm podman curl; do
  if command -v "$tool" >/dev/null 2>&1; then
    "$tool" --version | head -n 1
  else
    echo "MISSING: $tool (see README)" >&2
    failed=1
  fi
done
if command -v node >/dev/null 2>&1; then
  node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)' || {
    echo "Node.js 22+ required; recommended major version is in .nvmrc." >&2
    failed=1
  }
fi
if command -v podman >/dev/null 2>&1; then
  if podman info >/dev/null 2>&1; then
    echo "Podman connection: OK"
  else
    echo "Podman unavailable. On macOS check podman machine list/start." >&2
    failed=1
  fi
fi
if [ ! -f .env ]; then
  echo "PENDING: copy .env.example to .env and configure credentials."
fi
if [ -x backend/.venv/bin/python ]; then
  backend/.venv/bin/python --version
else
  echo "PENDING: make install creates backend/.venv with Python 3.11 and frontend dependencies."
fi
echo "No dependencies, containers or database records were changed."
exit "$failed"
