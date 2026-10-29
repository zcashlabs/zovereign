#!/usr/bin/env bash

set -Eeuo pipefail

SNAPSHOT_MANIFEST="${SNAPSHOT_MANIFEST:-https://zakura.valargroup.dev/mainnet/snapshots.json}"
SNAPSHOT_URL="${SNAPSHOT_URL:-}"
SNAPSHOT_SHA256="${SNAPSHOT_SHA256:-}"
SNAPSHOT_SIZE_BYTES="${SNAPSHOT_SIZE_BYTES:-}"
DOWNLOAD_CHUNK_BYTES="${DOWNLOAD_CHUNK_BYTES:-268435456}"
ZAKURA_DIR="${ZAKURA_DIR:-/srv/lightwalletd/zakura}"
RUNTIME_DIR="${RUNTIME_DIR:-/srv/lightwalletd/runtime}"
COMPOSE_FILE="${COMPOSE_FILE:-$RUNTIME_DIR/compose.yaml}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: this bootstrap must run as root" >&2
  exit 1
fi

for command_name in cat curl docker jq sha256sum stat tar tee zstd; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ -z "$SNAPSHOT_URL" || -z "$SNAPSHOT_SHA256" || -z "$SNAPSHOT_SIZE_BYTES" ]]; then
  LATEST_SNAPSHOT="$(curl --fail --location --silent --show-error "$SNAPSHOT_MANIFEST")"
  SNAPSHOT_URL="$(jq -er 'map(select(.snapshot_kind == "archive" and (.roles | index("latest")))) | first | .url' <<<"$LATEST_SNAPSHOT")"
  SNAPSHOT_SHA256="$(jq -er 'map(select(.snapshot_kind == "archive" and (.roles | index("latest")))) | first | .sha256' <<<"$LATEST_SNAPSHOT")"
  SNAPSHOT_SIZE_BYTES="$(jq -er 'map(select(.snapshot_kind == "archive" and (.roles | index("latest")))) | first | .size_bytes' <<<"$LATEST_SNAPSHOT")"
fi

if [[ ! "$SNAPSHOT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "error: invalid snapshot SHA-256 in manifest" >&2
  exit 1
fi

if [[ ! "$SNAPSHOT_SIZE_BYTES" =~ ^[1-9][0-9]*$ ]] || [[ ! "$DOWNLOAD_CHUNK_BYTES" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: invalid snapshot or chunk size" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "error: the Docker Compose plugin is required" >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

if [[ -e "$ZAKURA_DIR/state" || -e "$ZAKURA_DIR/.archive-bootstrap-complete" ]]; then
  echo "error: Zakura state already exists in $ZAKURA_DIR" >&2
  exit 1
fi

mkdir -p "$ZAKURA_DIR" /srv/lightwalletd/lightwalletd "$RUNTIME_DIR"
HASH_FILE="$(mktemp)"
CHUNK_FILE="$RUNTIME_DIR/snapshot-download.chunk"
SUCCESS=0
cleanup() {
  rm -f "$HASH_FILE" "$CHUNK_FILE"
  if [[ "$SUCCESS" != "1" ]]; then
    rm -rf "$ZAKURA_DIR/state"
  fi
}
trap cleanup EXIT

download_snapshot() {
  local start=0
  local end expected actual attempt

  while (( start < SNAPSHOT_SIZE_BYTES )); do
    end=$((start + DOWNLOAD_CHUNK_BYTES - 1))
    if (( end >= SNAPSHOT_SIZE_BYTES )); then
      end=$((SNAPSHOT_SIZE_BYTES - 1))
    fi
    expected=$((end - start + 1))

    for attempt in {1..10}; do
      rm -f "$CHUNK_FILE"
      if curl --http1.1 --fail --location --silent --show-error \
        --range "$start-$end" --output "$CHUNK_FILE" "$SNAPSHOT_URL"; then
        actual="$(stat -c '%s' "$CHUNK_FILE")"
        if (( actual == expected )); then
          break
        fi
        echo "warning: chunk $start-$end had $actual bytes; expected $expected" >&2
      fi
      if (( attempt == 10 )); then
        echo "error: failed to download chunk $start-$end after 10 attempts" >&2
        return 1
      fi
      sleep $((attempt * 2))
    done

    cat "$CHUNK_FILE"
    start=$((end + 1))
    printf 'Downloaded %d of %d bytes (%d%%)\n' \
      "$start" "$SNAPSHOT_SIZE_BYTES" "$((start * 100 / SNAPSHOT_SIZE_BYTES))" >&2
  done
}

echo "Streaming and extracting the Zakura archive snapshot in retryable HTTP/1.1 chunks..."
download_snapshot \
  | tee >(sha256sum | awk '{print $1}' > "$HASH_FILE") \
  | zstd --decompress --stdout \
  | tar --extract --directory "$ZAKURA_DIR"

for _ in {1..30}; do
  [[ -s "$HASH_FILE" ]] && break
  sleep 1
done
ACTUAL_SHA256="$(<"$HASH_FILE")"
if [[ "$ACTUAL_SHA256" != "$SNAPSHOT_SHA256" ]]; then
  echo "error: snapshot checksum mismatch" >&2
  exit 1
fi

if [[ ! -d "$ZAKURA_DIR/state" ]]; then
  echo "error: extracted snapshot does not contain Zakura state" >&2
  exit 1
fi

touch "$ZAKURA_DIR/.archive-bootstrap-complete"
chown -R 10001:10001 "$ZAKURA_DIR"
docker compose --file "$COMPOSE_FILE" up --detach
SUCCESS=1
echo "Zakura archive bootstrap verified; node services started."
