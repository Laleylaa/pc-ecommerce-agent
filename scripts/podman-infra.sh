#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./scripts/local-config.sh
NETWORK="$PODMAN_NETWORK"
POSTGRES_PUBLISH_HOST="$POSTGRES_BIND_HOST"
if [[ "$POSTGRES_PUBLISH_HOST" == *:* ]]; then
  POSTGRES_PUBLISH_HOST="[$POSTGRES_PUBLISH_HOST]"
fi

usage() {
  cat <<EOF
Usage: ./scripts/podman-infra.sh <command>

Commands:
  up              Start PostgreSQL
  down            Remove containers but keep volumes
  stop            Stop containers
  ps              Show service containers
  logs <service>  Follow PostgreSQL logs
  reset           Remove containers and volumes; requires CONFIRM_RESET=1
EOF
}

ensure_podman() {
  command -v podman >/dev/null 2>&1 || {
    echo "podman is not installed or not on PATH" >&2
    exit 1
  }
  podman info >/dev/null
}

ensure_network() {
  podman network exists "$NETWORK" >/dev/null 2>&1 || podman network create "$NETWORK" >/dev/null
}

ensure_volume() {
  local volume="$1"
  podman volume exists "$volume" >/dev/null 2>&1 || podman volume create "$volume" >/dev/null
}

is_running() {
  [ "$(podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" = "true" ]
}

start_existing() {
  local container="$1"
  if is_running "$container"; then
    echo "$container already running"
  else
    podman start "$container" >/dev/null
    echo "$container started"
  fi
}

assert_existing_port() {
  local container="$1" internal="$2" expected="$3"
  if [ "$(podman port "$container" "$internal/tcp")" != "${POSTGRES_PUBLISH_HOST}:${expected}" ]; then
    echo "$container does not publish expected binding ${POSTGRES_PUBLISH_HOST}:${expected}." >&2
    echo "Stop the application and recreate the container with its existing volume; see README. No container was changed." >&2
    exit 1
  fi
}

run_postgres() {
  ensure_volume "$POSTGRES_VOLUME"
  if podman container exists "$POSTGRES_CONTAINER"; then
    assert_existing_port "$POSTGRES_CONTAINER" 5432 "$POSTGRES_PORT"
    start_existing "$POSTGRES_CONTAINER"
    return
  fi

  podman run -d \
    --name "$POSTGRES_CONTAINER" \
    --network "$NETWORK" \
    -p "${POSTGRES_PUBLISH_HOST}:${POSTGRES_PORT}:5432" \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    --health-cmd "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}" \
    --health-interval 5s \
    --health-timeout 5s \
    --health-retries 10 \
    -v "${POSTGRES_VOLUME}:/var/lib/postgresql/data" \
    "$POSTGRES_IMAGE" >/dev/null
  echo "$POSTGRES_CONTAINER created"
}

wait_for_postgres() {
  for _ in $(seq 1 60); do
    if podman exec "$POSTGRES_CONTAINER" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      echo "postgres ready"
      return
    fi
    sleep 1
  done
  echo "postgres did not become ready in time" >&2
  exit 1
}

up() {
  ensure_podman
  ensure_network
  run_postgres
  wait_for_postgres
  ps
}

stop() {
  ensure_podman
  for container in "$POSTGRES_CONTAINER"; do
    if podman container exists "$container"; then
      podman stop "$container" >/dev/null || true
      echo "$container stopped"
    fi
  done
}

down() {
  ensure_podman
  for container in "$POSTGRES_CONTAINER"; do
    if podman container exists "$container"; then
      podman rm -f "$container" >/dev/null || true
      echo "$container removed"
    fi
  done
}

ps() {
  podman ps -a --filter "name=^${POSTGRES_CONTAINER}$" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
}

container_for() {
  case "${1:-}" in
    postgres) printf '%s\n' "$POSTGRES_CONTAINER" ;;
    *)
      echo "unknown service: ${1:-}" >&2
      echo "expected service: postgres" >&2
      exit 1
      ;;
  esac
}

logs() {
  ensure_podman
  podman logs -f "$(container_for "${1:-}")"
}

reset() {
  if [ "${CONFIRM_RESET:-}" != "1" ]; then
    echo "reset removes local Podman volumes; rerun with CONFIRM_RESET=1 to confirm" >&2
    exit 1
  fi

  down
  for volume in "$POSTGRES_VOLUME"; do
    if podman volume exists "$volume"; then
      podman volume rm "$volume" >/dev/null
      echo "$volume removed"
    fi
  done
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  stop) stop ;;
  ps)
    ensure_podman
    ps
    ;;
  logs) logs "${2:-}" ;;
  reset) reset ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
