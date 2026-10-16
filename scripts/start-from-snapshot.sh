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

if [[ "$SNAPSHOT_SOURCE" == http://* || "$SNAPSHOT_SOURCE" == https://* ]]; then
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

WORK_DIR=""
ARCHIVE_PATH=""
CHECKSUM_PATH=""
OWNER_LABEL=""
ROLE_LABEL="org.zcashlabs.zovereign.role"
VOLUME_ATTEMPTED=0
HELPER_ATTEMPTED=0
CHECK_ATTEMPTED=0
CONTAINER_ATTEMPTED=0
SUCCESS=0

# Labels recover our resources even if Docker created them but its command
# failed before returning an ID. Remove containers by ID, never a reused name.
remove_owned_containers() {
  local role="$1" ids id failed=0
  if ! ids="$(docker container ls --all --quiet \
    --filter "label=$OWNER_LABEL" --filter "label=$ROLE_LABEL=$role")"; then
    echo "error: cleanup could not list owned $role containers" >&2
    return 1
  fi
  for id in $ids; do
    if ! docker container rm --force "$id" >/dev/null; then
      echo "error: cleanup could not remove $role container $id" >&2
      failed=1
    fi
  done
  return "$failed"
}

remove_owned_volume() {
  local volumes volume failed=0
  if ! volumes="$(docker volume ls --quiet --filter "label=$OWNER_LABEL")"; then
    echo "error: cleanup could not list owned volumes" >&2
    return 1
  fi
  for volume in $volumes; do
    if [[ "$volume" != "$VOLUME_NAME" ]]; then
      echo "error: cleanup refusing unexpected volume $volume" >&2
      failed=1
    elif ! docker volume rm "$volume" >/dev/null; then
      echo "error: cleanup could not remove volume $volume" >&2
      failed=1
    fi
  done
  return "$failed"
}

cleanup() {
  local exit_code=$? cleanup_failed=0
  trap - EXIT
  trap '' INT TERM

  if [[ "$HELPER_ATTEMPTED" == "1" ]]; then
    if ! remove_owned_containers helper; then cleanup_failed=1; fi
  fi
  if [[ "$CHECK_ATTEMPTED" == "1" ]]; then
    if ! remove_owned_containers check; then cleanup_failed=1; fi
  fi

  if [[ "$SUCCESS" != "1" ]]; then
    if [[ "$CONTAINER_ATTEMPTED" == "1" ]]; then
      if ! remove_owned_containers target; then cleanup_failed=1; fi
    fi
    if [[ "$VOLUME_ATTEMPTED" == "1" ]]; then
      if ! remove_owned_volume; then cleanup_failed=1; fi
    fi
  fi

  if [[ -n "$WORK_DIR" ]] && ! rm -rf "$WORK_DIR"; then
    echo "error: cleanup could not remove temporary directory $WORK_DIR" >&2
    cleanup_failed=1
  fi
  if [[ "$exit_code" == "0" && "$cleanup_failed" != "0" ]]; then
    exit_code=1
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

WORK_DIR="$(mktemp -d)"
OWNER_LABEL="org.zcashlabs.zovereign.restore=${WORK_DIR##*/}"

if [[ "$SNAPSHOT_SOURCE" == s3://* ]]; then
  ARCHIVE_PATH="$WORK_DIR/snapshot.tar.zst"
  CHECKSUM_PATH="$WORK_DIR/snapshot.tar.zst.sha256"
  echo "Downloading snapshot from S3..."
  aws s3 cp --only-show-errors "$SNAPSHOT_SOURCE" "$ARCHIVE_PATH"
  aws s3 cp --only-show-errors "${SNAPSHOT_SOURCE}.sha256" "$CHECKSUM_PATH"
elif [[ "$SNAPSHOT_SOURCE" == http://* || "$SNAPSHOT_SOURCE" == https://* ]]; then
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
  EXPECTED_HASH="$(awk 'NR == 1 {print tolower($1)}' "$CHECKSUM_PATH")"
  if [[ ! "$EXPECTED_HASH" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "error: checksum sidecar does not contain a valid SHA-256 hash" >&2
    exit 1
  fi

  echo "Verifying SHA-256..."
  ACTUAL_HASH="$(sha256sum "$ARCHIVE_PATH" | awk '{print tolower($1)}')"
  if [[ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]]; then
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
VOLUME_ATTEMPTED=1
docker volume create --label "$OWNER_LABEL" "$VOLUME_NAME" >/dev/null
if [[ "$(docker volume inspect --format '{{index .Labels "org.zcashlabs.zovereign.restore"}}' "$VOLUME_NAME")" != "${OWNER_LABEL#*=}" ]]; then
  echo "error: volume '$VOLUME_NAME' was not created by this restore; refusing to use it" >&2
  exit 1
fi

RESTORE_HELPER="${CONTAINER_NAME}-restore-${WORK_DIR##*/}"
HELPER_ATTEMPTED=1
docker create \
  --name "$RESTORE_HELPER" \
  --label "$OWNER_LABEL" --label "$ROLE_LABEL=helper" \
  --volume "$VOLUME_NAME:$DATA_DIR" \
  "$IMAGE" >/dev/null

echo "Extracting snapshot into $VOLUME_NAME..."
zstd --decompress --stdout "$ARCHIVE_PATH" \
  | docker cp - "$RESTORE_HELPER:$DATA_DIR"
remove_owned_containers helper

CHECK_ATTEMPTED=1
if ! docker run --rm \
  --name "${CONTAINER_NAME}-check-${WORK_DIR##*/}" \
  --label "$OWNER_LABEL" --label "$ROLE_LABEL=check" \
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
  --label "$OWNER_LABEL" --label "$ROLE_LABEL=target"
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
CONTAINER_ATTEMPTED=1
docker "${DOCKER_ARGS[@]}" >/dev/null

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
