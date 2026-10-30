#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <snapshot.tar.zst>" >&2
  exit 2
fi

ARCHIVE_PATH="$(realpath "$1")"
SNAPSHOT_DIR="$(dirname "$ARCHIVE_PATH")"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://165-232-183-161.sslip.io}"
LIGHTWALLETD_VERSION="${LIGHTWALLETD_VERSION:-v0.6.0}"
RETAIN_ARCHIVES="${RETAIN_ARCHIVES:-3}"

for command_name in awk basename find mktemp mv realpath rm sha256sum sort stat; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ ! "$RETAIN_ARCHIVES" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: RETAIN_ARCHIVES must be a positive integer" >&2
  exit 1
fi

CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
INFO_PATH="${ARCHIVE_PATH}.info"
for artifact in "$ARCHIVE_PATH" "$CHECKSUM_PATH" "$INFO_PATH"; do
  if [[ ! -s "$artifact" ]]; then
    echo "error: required artifact is missing or empty: $artifact" >&2
    exit 1
  fi
done

EXPECTED_HASH="$(awk 'NR == 1 {print $1}' "$CHECKSUM_PATH")"
ACTUAL_HASH="$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')"
if [[ ! "$EXPECTED_HASH" =~ ^[0-9a-fA-F]{64}$ ]] || [[ "${EXPECTED_HASH,,}" != "${ACTUAL_HASH,,}" ]]; then
  echo "error: local snapshot checksum validation failed" >&2
  exit 1
fi

ARCHIVE_NAME="$(basename "$ARCHIVE_PATH")"
if [[ "$ARCHIVE_NAME" =~ ([0-9]{8}T[0-9]{6}Z)\.tar\.zst$ ]]; then
  CREATED_AT="${BASH_REMATCH[1]}"
else
  echo "error: snapshot filename does not contain the expected UTC timestamp" >&2
  exit 1
fi

ARCHIVE_SIZE="$(stat -c '%s' "$ARCHIVE_PATH")"
MANIFEST_TEMP="$(mktemp --tmpdir="$SNAPSHOT_DIR" .latest.json.XXXXXX)"
cleanup() {
  rm -f "$MANIFEST_TEMP"
}
trap cleanup EXIT

cat > "$MANIFEST_TEMP" <<EOF
{
  "format_version": 1,
  "network": "mainnet",
  "lightwalletd_version": "$LIGHTWALLETD_VERSION",
  "created_at": "$CREATED_AT",
  "snapshot_url": "$PUBLIC_BASE_URL/$ARCHIVE_NAME",
  "checksum_url": "$PUBLIC_BASE_URL/$ARCHIVE_NAME.sha256",
  "info_url": "$PUBLIC_BASE_URL/$ARCHIVE_NAME.info",
  "sha256": "${ACTUAL_HASH,,}",
  "size": $ARCHIVE_SIZE
}
EOF
chmod 0644 "$MANIFEST_TEMP"
mv -f "$MANIFEST_TEMP" "$SNAPSHOT_DIR/latest.json"

mapfile -t ARCHIVES < <(find "$SNAPSHOT_DIR" -maxdepth 1 -type f \
  -name 'lightwalletd-mainnet-*.tar.zst' -printf '%f\n' | sort -r)
for ((index = RETAIN_ARCHIVES; index < ${#ARCHIVES[@]}; index++)); do
  expired="$SNAPSHOT_DIR/${ARCHIVES[$index]}"
  rm -f "$expired" "${expired}.sha256" "${expired}.info"
  echo "Removed expired local snapshot: ${ARCHIVES[$index]}"
done

echo "Published $PUBLIC_BASE_URL/latest.json"
