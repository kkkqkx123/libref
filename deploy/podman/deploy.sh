#!/usr/bin/env sh
# Podman-native deployment: build the image, prepare an isolated network and
# volumes, then replace the libref and Qdrant containers with fresh ones.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
DEPLOY_DIR="$ROOT/deploy"
ENV_FILE=${LIBREF_ENV_FILE:-$DEPLOY_DIR/libref.env}
IMAGE=${LIBREF_IMAGE:-libref:latest}
NETWORK=${LIBREF_PODMAN_NETWORK:-libref}
DATA_VOLUME=${LIBREF_DATA_VOLUME:-libref-data}
QDRANT_VOLUME=${LIBREF_QDRANT_VOLUME:-qdrant-data}
QDRANT_IMAGE=${LIBREF_QDRANT_IMAGE:-qdrant/qdrant:v1.20.1}
BIND_HOST=${LIBREF_BIND_HOST:-127.0.0.1}
HOST_PORT=${LIBREF_HOST_PORT:-8001}

if [ ! -f "$ENV_FILE" ]; then
    echo "missing env file: $ENV_FILE" >&2
    echo "copy deploy/env.prod.example to deploy/libref.env first" >&2
    exit 1
fi

wait_health() {
    name=$1
    attempts=${2:-40}
    i=0
    while [ "$i" -lt "$attempts" ]; do
        if podman healthcheck run "$name" >/dev/null 2>&1; then
            return 0
        fi
        i=$((i + 1))
        sleep 3
    done
    echo "healthcheck timeout: $name" >&2
    return 1
}

if ! podman network exists "$NETWORK" >/dev/null 2>&1; then
    podman network create "$NETWORK" >/dev/null
fi

podman volume exists "$DATA_VOLUME" >/dev/null 2>&1 || podman volume create "$DATA_VOLUME" >/dev/null
podman volume exists "$QDRANT_VOLUME" >/dev/null 2>&1 || podman volume create "$QDRANT_VOLUME" >/dev/null

podman build -t "$IMAGE" -f "$ROOT/Dockerfile" "$ROOT"

for name in libref qdrant; do
    if podman container exists "$name"; then
        podman rm -f "$name" >/dev/null
    fi
done

podman run -d --name qdrant --network "$NETWORK" --restart unless-stopped \
    -e "QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY:-}" \
    -v "$QDRANT_VOLUME:/qdrant/storage:U" \
    --health-cmd "bash -c ': < /dev/tcp/127.0.0.1/6333'" \
    --health-interval 15s \
    --health-timeout 5s \
    --health-retries 5 \
    "$QDRANT_IMAGE" >/dev/null

wait_health qdrant

podman run -d --name libref --network "$NETWORK" --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -e LIBREF_DB_PATH=/data/libref.db \
    -e LIBREF_SNAPSHOT_DIR=/data/snapshots \
    -e LIBREF_REPOS_DIR=/data/repos \
    -e "LIBREF_DEPLOYMENT_ID=${LIBREF_DEPLOYMENT_ID:-libref-podman}" \
    -e LIBREF_QDRANT_URL=http://qdrant:6333 \
    -v "$DATA_VOLUME:/data:U" \
    -p "$BIND_HOST:$HOST_PORT:8001" \
    --stop-timeout 35 \
    --health-cmd "python -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8001/health\")'" \
    --health-interval 30s \
    --health-timeout 5s \
    --health-retries 3 \
    "$IMAGE" >/dev/null

wait_health libref

echo "libref is serving at http://$BIND_HOST:$HOST_PORT"
echo "frontend shell: http://$BIND_HOST:$HOST_PORT/"
