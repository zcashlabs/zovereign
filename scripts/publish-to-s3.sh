#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <snapshot.tar.zst>" >&2
  exit 2
fi

ARCHIVE_PATH="$(realpath "$1")"
S3_BUCKET="${S3_BUCKET:-}"
S3_PREFIX="${S3_PREFIX:-mainnet/v0.5.4}"
DELETE_LOCAL_AFTER_UPLOAD="${DELETE_LOCAL_AFTER_UPLOAD:-0}"

for command_name in aws sha256sum stat mktemp; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ -z "$S3_BUCKET" ]]; then
  echo "error: S3_BUCKET is required" >&2
  exit 1
fi

if [[ ! -s "$ARCHIVE_PATH" ]]; then
  echo "error: snapshot archive is missing or empty: $ARCHIVE_PATH" >&2
  exit 1
fi

CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
INFO_PATH="${ARCHIVE_PATH}.info"
for sidecar in "$CHECKSUM_PATH" "$INFO_PATH"; do
  if [[ ! -s "$sidecar" ]]; then
    echo "error: required sidecar is missing or empty: $sidecar" >&2
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
  TIMESTAMP="${BASH_REMATCH[1]}"
else
  echo "error: snapshot filename does not contain the expected UTC timestamp: $ARCHIVE_NAME" >&2
  exit 1
fi

OBJECT_PREFIX="${S3_PREFIX%/}/snapshots/$TIMESTAMP"
ARCHIVE_KEY="$OBJECT_PREFIX/snapshot.tar.zst"
CHECKSUM_KEY="${ARCHIVE_KEY}.sha256"
INFO_KEY="${ARCHIVE_KEY}.info"
LATEST_KEY="${S3_PREFIX%/}/latest.json"

echo "Uploading snapshot to s3://$S3_BUCKET/$ARCHIVE_KEY..."
aws s3 cp --only-show-errors "$ARCHIVE_PATH" "s3://$S3_BUCKET/$ARCHIVE_KEY" \
  --content-type application/zstd \
  --cache-control 'public,max-age=31536000,immutable'
aws s3 cp --only-show-errors "$CHECKSUM_PATH" "s3://$S3_BUCKET/$CHECKSUM_KEY" \
  --content-type text/plain \
  --cache-control 'public,max-age=31536000,immutable'
aws s3 cp --only-show-errors "$INFO_PATH" "s3://$S3_BUCKET/$INFO_KEY" \
  --content-type text/plain \
  --cache-control 'public,max-age=31536000,immutable'

aws s3api head-object --bucket "$S3_BUCKET" --key "$ARCHIVE_KEY" >/dev/null
aws s3api head-object --bucket "$S3_BUCKET" --key "$CHECKSUM_KEY" >/dev/null
aws s3api head-object --bucket "$S3_BUCKET" --key "$INFO_KEY" >/dev/null

MANIFEST_PATH="$(mktemp)"
cleanup() {
  rm -f "$MANIFEST_PATH"
}
trap cleanup EXIT

ARCHIVE_SIZE="$(stat -c '%s' "$ARCHIVE_PATH")"
cat > "$MANIFEST_PATH" <<EOF
{
  "format_version": 1,
  "network": "mainnet",
  "lightwalletd_version": "v0.5.4",
  "created_at": "$TIMESTAMP",
  "snapshot_key": "$ARCHIVE_KEY",
  "checksum_key": "$CHECKSUM_KEY",
  "info_key": "$INFO_KEY",
  "sha256": "${ACTUAL_HASH,,}",
  "size": $ARCHIVE_SIZE
}
EOF

# Publishing latest.json last makes it the commit point for consumers.
aws s3 cp --only-show-errors "$MANIFEST_PATH" "s3://$S3_BUCKET/$LATEST_KEY" \
  --content-type application/json \
  --cache-control 'no-cache,max-age=0'

echo "Published s3://$S3_BUCKET/$ARCHIVE_KEY"
echo "Updated   s3://$S3_BUCKET/$LATEST_KEY"

if [[ "$DELETE_LOCAL_AFTER_UPLOAD" == "1" ]]; then
  rm -f "$ARCHIVE_PATH" "$CHECKSUM_PATH" "$INFO_PATH"
  echo "Removed local snapshot artifacts after successful publication."
fi
