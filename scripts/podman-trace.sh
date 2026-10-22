#!/usr/bin/env bash
# Independent local Phoenix only. Never reads .env or touches business containers.
set -euo pipefail
name=pc-ecommerce-agent-phoenix
volume=pc-ecommerce-agent-phoenix-data
image=docker.io/arizephoenix/phoenix:version-20.23.0
label=pc-ecommerce-agent.trace
command -v podman >/dev/null || { echo 'Podman is required; see README.' >&2; exit 1; }
exists() { podman container exists "$name"; }
owned() {
  test "$(podman inspect --format '{{index .Config.Labels "pc-ecommerce-agent.trace"}}' "$name")" = p4 || {
    echo "Refusing to modify unrelated container: $name" >&2; exit 1;
  }
  test "$(podman inspect --format '{{.Config.Image}}' "$name")" = "$image" || {
    echo 'Image differs; inspect manually before rebuilding. Data is preserved.' >&2; exit 1;
  }
  test "$(podman inspect --format '{{range (index .HostConfig.PortBindings "6006/tcp")}}{{.HostIP}}:{{.HostPort}}{{end}}' "$name")" = '127.0.0.1:6006' || {
    echo 'Unexpected port binding; refusing to start or change container.' >&2; exit 1;
  }
}
case "${1:-status}" in
  up)
    if exists; then
      owned
      podman start "$name"
    else
      # Refuse an occupied host port, even when the container has not been created.
      if curl --noproxy '*' --connect-timeout 1 --max-time 2 -s -o /dev/null http://127.0.0.1:6006/; then
        echo 'Port 6006 is occupied; no resources changed.' >&2; exit 1
      fi
      if podman volume exists "$volume"; then
        test "$(podman volume inspect --format '{{index .Labels "pc-ecommerce-agent.trace"}}' "$volume")" = p4 || {
          echo 'Volume exists without ownership label; refusing reuse.' >&2; exit 1;
        }
      else
        podman volume create --label "$label=p4" "$volume"
      fi
      podman run -d --name "$name" --label "$label=p4" \
        -p 127.0.0.1:6006:6006 -v "$volume:/data" \
        -e PHOENIX_WORKING_DIR=/data -e PHOENIX_SQL_DATABASE_URL=sqlite:////data/phoenix.db \
        -e PHOENIX_ALLOW_EXTERNAL_RESOURCES=false -e PHOENIX_ENABLE_PROMETHEUS=false -e PHOENIX_TELEMETRY_ENABLED=false "$image"
    fi
    for _ in {1..30}; do
      if curl --noproxy '*' --connect-timeout 1 --max-time 2 -fsS \
        http://127.0.0.1:6006/healthz >/dev/null 2>&1; then
        echo 'Phoenix: http://127.0.0.1:6006  project: pc-ecommerce-agent-local'; exit 0
      fi
      sleep 1
    done
    echo "Phoenix not ready; inspect: podman logs $name" >&2; exit 1
    ;;
  stop) if exists; then owned; podman stop --time 5 "$name"; fi ;;
  status)
    if exists; then
      podman inspect --format 'name={{.Name}} state={{.State.Status}} image={{.Config.Image}}' "$name"
      podman port "$name"
    else echo 'Phoenix container not created'; fi
    ;;
  *) echo 'Usage: scripts/podman-trace.sh up|stop|status' >&2; exit 2 ;;
esac
