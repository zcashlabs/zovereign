#!/usr/bin/env bash

set -Eeuo pipefail

SNAPSHOT_MANIFEST="${SNAPSHOT_MANIFEST:-https://zakura.valargroup.dev/mainnet/snapshots.json}"
SNAPSHOT_URL="${SNAPSHOT_URL:-}"
SNAPSHOT_SHA256="${SNAPSHOT_SHA256:-}"
ZAKURA_DIR="${ZAKURA_DIR:-/srv/lightwalletd/zakura}"
RUNTIME_DIR="${RUNTIME_DIR:-/srv/lightwalletd/runtime}"
COMPOSE_FILE="${COMPOSE_FILE:-$RUNTIME_DIR/compose.yaml}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: this bootstrap must run as root" >&2
  exit 1
fi

for command_name in curl docker jq sha256sum tar tee zstd; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ -z "$SNAPSHOT_URL" || -z "$SNAPSHOT_SHA256" ]]; then
  LATEST_SNAPSHOT="$(curl --fail --location --silent --show-error "$SNAPSHOT_MANIFEST")"
  SNAPSHOT_URL="$(jq -er 'map(select(.snapshot_kind == "archive" and (.roles | index("latest")))) | first | .url' <<<"$LATEST_SNAPSHOT")"
  SNAPSHOT_SHA256="$(jq -er 'map(select(.snapshot_kind == "archive" and (.roles | index("latest")))) | first | .sha256' <<<"$LATEST_SNAPSHOT")"
fi

if [[ ! "$SNAPSHOT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "error: invalid snapshot SHA-256 in manifest" >&2
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
cleanup() {
  rm -f "$HASH_FILE"
}
trap cleanup EXIT

echo "Streaming and extracting the Zakura archive snapshot..."
curl --fail --location --silent --show-error "$SNAPSHOT_URL" \
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
  rm -rf "$ZAKURA_DIR/state"
  exit 1
fi

if [[ ! -d "$ZAKURA_DIR/state" ]]; then
  echo "error: extracted snapshot does not contain Zakura state" >&2
  exit 1
fi

touch "$ZAKURA_DIR/.archive-bootstrap-complete"
chown -R 10001:10001 "$ZAKURA_DIR"
docker compose --file "$COMPOSE_FILE" up --detach
echo "Zakura archive bootstrap verified; node services started."
