#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

dotenv_get() {
  local key="$1"
  local default="$2"
  local value="${!key-}"

  if [ -z "$value" ] && [ -f .env ]; then
    value="$(
      { grep -E "^[[:space:]]*${key}=" .env 2>/dev/null || true; } \
        | tail -n 1 \
        | sed -E "s/^[[:space:]]*${key}=//; s/^['\"]//; s/['\"]$//"
    )"
  fi

  printf '%s' "${value:-$default}"
}

PROJECT_NAME="$(dotenv_get PODMAN_PROJECT_NAME pc-agent)"
NETWORK="$(dotenv_get PODMAN_NETWORK "${PROJECT_NAME}-net")"
POSTGRES_CONTAINER="$(dotenv_get POSTGRES_CONTAINER "${PROJECT_NAME}-postgres")"
REDIS_CONTAINER="$(dotenv_get REDIS_CONTAINER "${PROJECT_NAME}-redis")"
CHROMA_CONTAINER="$(dotenv_get CHROMA_CONTAINER "${PROJECT_NAME}-chroma")"
POSTGRES_VOLUME="$(dotenv_get POSTGRES_VOLUME "${PROJECT_NAME}-postgres-data")"
REDIS_VOLUME="$(dotenv_get REDIS_VOLUME "${PROJECT_NAME}-redis-data")"
CHROMA_VOLUME="$(dotenv_get CHROMA_VOLUME "${PROJECT_NAME}-chroma-data")"
POSTGRES_IMAGE="$(dotenv_get POSTGRES_IMAGE postgres:16-alpine)"
REDIS_IMAGE="$(dotenv_get REDIS_IMAGE redis:7-alpine)"
CHROMA_IMAGE="$(dotenv_get CHROMA_IMAGE docker.io/chromadb/chroma@sha256:bd21353aee6ccdf4a57bd91e6001626826700f3838e1f230d4aae75bfd4889a1)"
POSTGRES_PORT="$(dotenv_get POSTGRES_PORT 5432)"
REDIS_PORT="$(dotenv_get REDIS_PORT 6379)"
CHROMA_PORT="$(dotenv_get CHROMA_PORT 8001)"

POSTGRES_DB="$(dotenv_get POSTGRES_DB pc_agent)"
POSTGRES_USER="$(dotenv_get POSTGRES_USER pc_agent)"
POSTGRES_PASSWORD="$(dotenv_get POSTGRES_PASSWORD pc_agent)"

usage() {
  cat <<EOF
Usage: ./scripts/podman-infra.sh <command>

Commands:
  up              Create or start PostgreSQL, Redis, and ChromaDB
  down            Remove containers but keep volumes
  stop            Stop containers
  ps              Show service containers
  logs <service>  Follow logs for postgres, redis, or chroma
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
  if ! podman port "$container" "$internal/tcp" | grep -Eq ":${expected}$"; then
    echo "$container does not publish expected host port $expected. Check .env and existing container configuration." >&2
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
    -p "${POSTGRES_PORT}:5432" \
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

run_redis() {
  ensure_volume "$REDIS_VOLUME"
  if podman container exists "$REDIS_CONTAINER"; then
    assert_existing_port "$REDIS_CONTAINER" 6379 "$REDIS_PORT"
    start_existing "$REDIS_CONTAINER"
    return
  fi

  podman run -d \
    --name "$REDIS_CONTAINER" \
    --network "$NETWORK" \
    -p "${REDIS_PORT}:6379" \
    --health-cmd "redis-cli ping" \
    --health-interval 5s \
    --health-timeout 3s \
    --health-retries 10 \
    -v "${REDIS_VOLUME}:/data" \
    "$REDIS_IMAGE" >/dev/null
  echo "$REDIS_CONTAINER created"
}

run_chroma() {
  ensure_volume "$CHROMA_VOLUME"
  if podman container exists "$CHROMA_CONTAINER"; then
    assert_existing_port "$CHROMA_CONTAINER" 8000 "$CHROMA_PORT"
    start_existing "$CHROMA_CONTAINER"
    return
  fi

  podman run -d \
    --name "$CHROMA_CONTAINER" \
    --network "$NETWORK" \
    -p "${CHROMA_PORT}:8000" \
    -e IS_PERSISTENT=TRUE \
    -e PERSIST_DIRECTORY=/chroma/chroma \
    -v "${CHROMA_VOLUME}:/chroma/chroma" \
    "$CHROMA_IMAGE" >/dev/null
  echo "$CHROMA_CONTAINER created"
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

wait_for_redis() {
  for _ in $(seq 1 30); do
    if podman exec "$REDIS_CONTAINER" redis-cli ping >/dev/null 2>&1; then
      echo "redis ready"
      return
    fi
    sleep 1
  done
  echo "redis did not become ready in time" >&2
  exit 1
}

wait_for_chroma() {
  command -v curl >/dev/null 2>&1 || { echo "curl is required for Chroma readiness" >&2; exit 1; }
  for _ in $(seq 1 30); do
    for api in v2 v1; do
      if curl --fail --silent --noproxy '*' --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:${CHROMA_PORT}/api/${api}/heartbeat" >/dev/null 2>&1; then
        echo "chroma ready"
        return
      fi
    done
    sleep 1
  done
  echo "chroma did not become ready; inspect ./scripts/podman-infra.sh logs chroma" >&2
  exit 1
}

up() {
  ensure_podman
  ensure_network
  run_postgres
  run_redis
  run_chroma
  wait_for_postgres
  wait_for_redis
  wait_for_chroma
  ps
}

stop() {
  ensure_podman
  ensure_podman
  for container in "$CHROMA_CONTAINER" "$REDIS_CONTAINER" "$POSTGRES_CONTAINER"; do
    if podman container exists "$container"; then
      podman stop "$container" >/dev/null || true
      echo "$container stopped"
    fi
  done
}

down() {
  ensure_podman
  ensure_podman
  for container in "$CHROMA_CONTAINER" "$REDIS_CONTAINER" "$POSTGRES_CONTAINER"; do
    if podman container exists "$container"; then
      podman rm -f "$container" >/dev/null || true
      echo "$container removed"
    fi
  done
}

ps() {
  podman ps -a --filter "name=^${POSTGRES_CONTAINER}$" --filter "name=^${REDIS_CONTAINER}$" --filter "name=^${CHROMA_CONTAINER}$" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
}

container_for() {
  case "${1:-}" in
    postgres) printf '%s\n' "$POSTGRES_CONTAINER" ;;
    redis) printf '%s\n' "$REDIS_CONTAINER" ;;
    chroma) printf '%s\n' "$CHROMA_CONTAINER" ;;
    *)
      echo "unknown service: ${1:-}" >&2
      echo "expected one of: postgres, redis, chroma" >&2
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
  for volume in "$CHROMA_VOLUME" "$REDIS_VOLUME" "$POSTGRES_VOLUME"; do
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
