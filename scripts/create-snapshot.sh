#!/usr/bin/env bash

set -Eeuo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-lightwalletd}"
DATA_DIR="${DATA_DIR:-/var/lib/lightwalletd}"
LIGHTWALLETD_VERSION="${LIGHTWALLETD_VERSION:-v0.5.4}"
EXPECTED_IMAGE="${EXPECTED_IMAGE:-electriccoinco/lightwalletd:${LIGHTWALLETD_VERSION}}"
COMPRESSION_LEVEL="${COMPRESSION_LEVEL:-3}"
OUTPUT_DIR="${1:-./snapshots}"

for command_name in docker zstd sha256sum date; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "error: Docker container does not exist: $CONTAINER_NAME" >&2
  exit 1
fi

if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
  echo "error: Docker container is not running: $CONTAINER_NAME" >&2
  exit 1
fi

SOURCE_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME")"
if [[ "$SOURCE_IMAGE" != "$EXPECTED_IMAGE" ]]; then
  echo "error: source image is '$SOURCE_IMAGE'; expected '$EXPECTED_IMAGE'" >&2
  echo "Set EXPECTED_IMAGE explicitly only if the snapshot consumer will use the same compatible image." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_NAME="lightwalletd-mainnet-${LIGHTWALLETD_VERSION}-${TIMESTAMP}.tar.zst"
ARCHIVE_PATH="$OUTPUT_DIR/$ARCHIVE_NAME"
PARTIAL_PATH="${ARCHIVE_PATH}.partial.$$"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
INFO_PATH="${ARCHIVE_PATH}.info"
NEEDS_RESTART=0

cleanup() {
  local exit_code=$?
  trap - EXIT

  rm -f "$PARTIAL_PATH"

  if [[ "$NEEDS_RESTART" == "1" ]]; then
    echo "Restarting $CONTAINER_NAME after interrupted or failed snapshot creation..." >&2
    docker start "$CONTAINER_NAME" >/dev/null || true
  fi

  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

LAST_BLOCK_LOG="$(docker logs --tail 500 "$CONTAINER_NAME" 2>&1 | grep 'Waiting for block:' | tail -n 1 || true)"

if [[ -n "$LAST_BLOCK_LOG" ]]; then
  echo "Latest source status: $LAST_BLOCK_LOG"
else
  echo "warning: no recent 'Waiting for block' line was found in the source logs" >&2
fi

echo "Stopping $CONTAINER_NAME to create a consistent snapshot..."
docker stop "$CONTAINER_NAME" >/dev/null
NEEDS_RESTART=1

echo "Archiving $DATA_DIR to $ARCHIVE_PATH..."
docker cp "$CONTAINER_NAME:$DATA_DIR/." - \
  | zstd -T0 "-$COMPRESSION_LEVEL" --force --quiet -o "$PARTIAL_PATH"

if [[ ! -s "$PARTIAL_PATH" ]]; then
  echo "error: snapshot archive is empty" >&2
  exit 1
fi

echo "Restarting $CONTAINER_NAME..."
docker start "$CONTAINER_NAME" >/dev/null
NEEDS_RESTART=0

for _ in {1..30}; do
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
    break
  fi
  sleep 1
done

if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
  echo "error: $CONTAINER_NAME did not return to the running state" >&2
  exit 1
fi

mv "$PARTIAL_PATH" "$ARCHIVE_PATH"
(
  cd "$OUTPUT_DIR"
  sha256sum "$ARCHIVE_NAME" > "$ARCHIVE_NAME.sha256"
)

ARCHIVE_SIZE="$(du -h "$ARCHIVE_PATH" | awk '{print $1}')"
{
  echo "format_version=0"
  echo "network=mainnet"
  echo "created_at=$TIMESTAMP"
  echo "source_container=$CONTAINER_NAME"
  echo "source_image=$SOURCE_IMAGE"
  echo "data_dir=$DATA_DIR"
  echo "archive=$ARCHIVE_NAME"
  echo "archive_size=$ARCHIVE_SIZE"
  if [[ -n "$LAST_BLOCK_LOG" ]]; then
    printf 'last_block_log=%s\n' "$LAST_BLOCK_LOG"
  fi
} > "$INFO_PATH"

chmod 0644 "$ARCHIVE_PATH" "$CHECKSUM_PATH" "$INFO_PATH"

echo
echo "Snapshot created successfully:"
echo "  Archive:  $ARCHIVE_PATH"
echo "  Size:     $ARCHIVE_SIZE"
echo "  Checksum: $CHECKSUM_PATH"
echo "  Info:     $INFO_PATH"
echo "  SHA-256:  $(awk '{print $1}' "$CHECKSUM_PATH")"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'archive_path=%s\n' "$ARCHIVE_PATH" >> "$GITHUB_OUTPUT"
fi
