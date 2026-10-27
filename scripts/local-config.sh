#!/usr/bin/env bash
# Sourced by infrastructure commands after changing to the repository root.
# Same dotenv parser and precedence as the application; no eval/source of .env.
if [ ! -x backend/.venv/bin/python ]; then
  echo 'Run make backend-install before using infrastructure commands.' >&2
  exit 1
fi
LOCAL_CONFIG_LOADED=false
while IFS= read -r -d '' config_key && IFS= read -r -d '' config_value; do
  printf -v "$config_key" '%s' "$config_value"
done < <(PYTHONPATH=backend backend/.venv/bin/python -m scripts.local_config)
if [ "$LOCAL_CONFIG_LOADED" != true ]; then
  echo 'Configuration could not be loaded; no containers were changed.' >&2
  exit 1
fi
unset config_key config_value
