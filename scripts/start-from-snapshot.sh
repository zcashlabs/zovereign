#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <snapshot-path-or-url>" >&2
  exit 2
fi

SNAPSHOT_SOURCE="$1"
CONTAINER_NAME="${CONTAINER_NAME:-lightwalletd}"
VOLUME_NAME="${VOLUME_NAME:-lightwalletd-data}"
IMAGE="${IMAGE:-electriccoinco/lightwalletd:v0.5.4}"
DATA_DIR="${DATA_DIR:-/var/lib/lightwalletd}"
DOCKER_NETWORK="${DOCKER_NETWORK:-}"
GRPC_PORT="${GRPC_PORT:-9067}"
RPC_HOST="${RPC_HOST:-}"
RPC_PORT="${RPC_PORT:-8232}"
RPC_USER="${RPC_USER:-lightwalletd}"
RPC_PASSWORD="${RPC_PASSWORD:-}"
START_TIMEOUT="${START_TIMEOUT:-60}"
FORCE="${FORCE:-0}"

for command_name in docker zstd sha256sum awk mktemp; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ "$SNAPSHOT_SOURCE" == s3://* ]] && ! command -v aws >/dev/null 2>&1; then
  echo "error: required command not found for S3 download: aws" >&2
  exit 1
fi

if [[ "$SNAPSHOT_SOURCE" == s3://* ]]; then
  ARCHIVE_NAME="${SNAPSHOT_SOURCE##*/}"
  ARCHIVE_PATH="$WORK_DIR/$ARCHIVE_NAME"
  CHECKSUM_PATH="$WORK_DIR/$ARCHIVE_NAME.sha256"

  echo "Downloading snapshot from S3..."
  aws s3 cp --only-show-errors "$SNAPSHOT_SOURCE" "$ARCHIVE_PATH"
  aws s3 cp --only-show-errors "${SNAPSHOT_SOURCE}.sha256" "$CHECKSUM_PATH"
elif [[ "$SNAPSHOT_SOURCE" == http://* || "$SNAPSHOT_SOURCE" == https://* ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "error: required command not found for URL download: curl" >&2
    exit 1
  fi
fi

if [[ -z "$RPC_HOST" ]]; then
  echo "error: RPC_HOST is required" >&2
  exit 1
fi

if [[ -z "$RPC_PASSWORD" ]]; then
  echo "error: RPC_PASSWORD is required" >&2
  exit 1
fi

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  if [[ "$FORCE" != "1" ]]; then
    echo "error: container '$CONTAINER_NAME' already exists; choose another CONTAINER_NAME" >&2
    echo "Set FORCE=1 only if it is safe to remove that container and its target volume." >&2
    exit 1
  fi
  echo "Removing existing container $CONTAINER_NAME because FORCE=1..."
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi

if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
  if [[ "$FORCE" != "1" ]]; then
    echo "error: volume '$VOLUME_NAME' already exists; choose another VOLUME_NAME" >&2
    echo "Set FORCE=1 only if it is safe to permanently remove that volume." >&2
    exit 1
  fi
  echo "Removing existing volume $VOLUME_NAME because FORCE=1..."
  docker volume rm "$VOLUME_NAME" >/dev/null
fi

WORK_DIR="$(mktemp -d)"
ARCHIVE_PATH=""
CHECKSUM_PATH=""
VOLUME_CREATED=0
CONTAINER_CREATED=0
SUCCESS=0

cleanup() {
  local exit_code=$?
  trap - EXIT

  rm -rf "$WORK_DIR"

  if [[ "$SUCCESS" != "1" ]]; then
    if [[ "$CONTAINER_CREATED" == "1" ]]; then
      docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$VOLUME_CREATED" == "1" ]]; then
      docker volume rm "$VOLUME_NAME" >/dev/null 2>&1 || true
    fi
  fi

  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$SNAPSHOT_SOURCE" == http://* || "$SNAPSHOT_SOURCE" == https://* ]]; then
  SOURCE_WITHOUT_QUERY="${SNAPSHOT_SOURCE%%\?*}"
  ARCHIVE_NAME="${SOURCE_WITHOUT_QUERY##*/}"
  if [[ -z "$ARCHIVE_NAME" ]]; then
    ARCHIVE_NAME="lightwalletd-snapshot.tar.zst"
  fi
  ARCHIVE_PATH="$WORK_DIR/$ARCHIVE_NAME"
  CHECKSUM_PATH="$WORK_DIR/$ARCHIVE_NAME.sha256"

  echo "Downloading snapshot..."
  curl --fail --location --retry 3 --output "$ARCHIVE_PATH" "$SNAPSHOT_SOURCE"

  echo "Looking for checksum sidecar..."
  if curl --fail --location --retry 2 --output "$CHECKSUM_PATH" "${SNAPSHOT_SOURCE}.sha256"; then
    echo "Checksum sidecar downloaded."
  else
    rm -f "$CHECKSUM_PATH"
    echo "warning: no checksum sidecar was available; continuing without checksum verification" >&2
  fi
else
  if [[ ! -f "$SNAPSHOT_SOURCE" ]]; then
    echo "error: snapshot file does not exist: $SNAPSHOT_SOURCE" >&2
    exit 1
  fi
  ARCHIVE_PATH="$(cd "$(dirname "$SNAPSHOT_SOURCE")" && pwd)/$(basename "$SNAPSHOT_SOURCE")"
  if [[ -f "${ARCHIVE_PATH}.sha256" ]]; then
    CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
  fi
fi

if [[ ! -s "$ARCHIVE_PATH" ]]; then
  echo "error: snapshot archive is empty" >&2
  exit 1
fi

if [[ -n "$CHECKSUM_PATH" && -f "$CHECKSUM_PATH" ]]; then
  EXPECTED_HASH="$(awk 'NR == 1 {print $1}' "$CHECKSUM_PATH")"
  if [[ ! "$EXPECTED_HASH" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "error: checksum sidecar does not contain a valid SHA-256 hash" >&2
    exit 1
  fi

  echo "Verifying SHA-256..."
  ACTUAL_HASH="$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')"
  if [[ "${ACTUAL_HASH,,}" != "${EXPECTED_HASH,,}" ]]; then
    echo "error: snapshot checksum mismatch" >&2
    echo "Expected: $EXPECTED_HASH" >&2
    echo "Actual:   $ACTUAL_HASH" >&2
    exit 1
  fi
  echo "Checksum verified."
fi

echo "Testing compressed archive..."
zstd --test --quiet "$ARCHIVE_PATH"

echo "Creating Docker volume $VOLUME_NAME..."
docker volume create "$VOLUME_NAME" >/dev/null
VOLUME_CREATED=1

RESTORE_HELPER="${CONTAINER_NAME}-restore-$$"
docker create \
  --name "$RESTORE_HELPER" \
  --volume "$VOLUME_NAME:$DATA_DIR" \
  "$IMAGE" >/dev/null

restore_helper_cleanup() {
  docker rm -f "$RESTORE_HELPER" >/dev/null 2>&1 || true
}
trap 'restore_helper_cleanup; exit 130' INT
trap 'restore_helper_cleanup; exit 143' TERM

echo "Extracting snapshot into $VOLUME_NAME..."
zstd --decompress --stdout "$ARCHIVE_PATH" \
  | docker cp - "$RESTORE_HELPER:$DATA_DIR"
restore_helper_cleanup
trap 'exit 130' INT
trap 'exit 143' TERM

if ! docker run --rm \
  --volume "$VOLUME_NAME:$DATA_DIR" \
  --entrypoint sh \
  "$IMAGE" \
  -c "test -f '$DATA_DIR/db/main/blocks' && test -f '$DATA_DIR/db/main/lengths'"; then
  echo "error: restored volume does not contain the expected lightwalletd mainnet database" >&2
  exit 1
fi

DOCKER_ARGS=(
  run --detach
  --name "$CONTAINER_NAME"
  --restart unless-stopped
  --publish "$GRPC_PORT:9067"
  --volume "$VOLUME_NAME:$DATA_DIR"
)

if [[ -n "$DOCKER_NETWORK" ]]; then
  DOCKER_ARGS+=(--network "$DOCKER_NETWORK")
fi

DOCKER_ARGS+=(
  "$IMAGE"
  --no-tls-very-insecure
  --grpc-bind-addr=0.0.0.0:9067
  --http-bind-addr=0.0.0.0:9068
  "--rpchost=$RPC_HOST"
  "--rpcport=$RPC_PORT"
  "--rpcuser=$RPC_USER"
  "--rpcpassword=$RPC_PASSWORD"
  "--data-dir=$DATA_DIR"
  --log-file=/dev/stdout
  --log-level=6
)

echo "Starting $CONTAINER_NAME from the restored snapshot..."
docker "${DOCKER_ARGS[@]}" >/dev/null
CONTAINER_CREATED=1

READY=0
for ((elapsed = 0; elapsed < START_TIMEOUT; elapsed++)); do
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" != "true" ]]; then
    echo "error: $CONTAINER_NAME stopped during startup" >&2
    docker logs --tail 50 "$CONTAINER_NAME" >&2 || true
    exit 1
  fi

  if (echo > "/dev/tcp/127.0.0.1/$GRPC_PORT") >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" != "1" ]]; then
  echo "error: gRPC port $GRPC_PORT did not become reachable within ${START_TIMEOUT}s" >&2
  docker logs --tail 50 "$CONTAINER_NAME" >&2 || true
  exit 1
fi

SUCCESS=1

echo
echo "lightwalletd started successfully from the snapshot."
echo "  Container: $CONTAINER_NAME"
echo "  Volume:    $VOLUME_NAME"
echo "  gRPC:      0.0.0.0:$GRPC_PORT"
echo
echo "Recent logs:"
docker logs --tail 20 "$CONTAINER_NAME" 2>&1 || true
echo
echo "Follow synchronization with:"
echo "  docker logs -f $CONTAINER_NAME"
