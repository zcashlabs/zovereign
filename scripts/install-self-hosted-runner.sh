#!/usr/bin/env bash

set -Eeuo pipefail

RUNNER_VERSION="${RUNNER_VERSION:-2.337.0}"
RUNNER_SHA256="${RUNNER_SHA256:-70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613}"
RUNNER_URL="${RUNNER_URL:-https://github.com/zcashlabs/zovereign}"
RUNNER_NAME="${RUNNER_NAME:-lightwalletd-mainnet-blr1-1}"
RUNNER_LABELS="${RUNNER_LABELS:-lightwalletd-snapshot}"
RUNNER_USER="${RUNNER_USER:-github-runner}"
RUNNER_DIR="${RUNNER_DIR:-/opt/actions-runner}"
RUNNER_TOKEN="${RUNNER_TOKEN:-}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: this installer must run as root" >&2
  exit 1
fi

for command_name in curl sha256sum tar useradd usermod; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ -z "$RUNNER_TOKEN" ]]; then
  echo "error: RUNNER_TOKEN is required" >&2
  exit 1
fi

if [[ -e "$RUNNER_DIR/.runner" ]]; then
  echo "error: a GitHub Actions runner is already configured in $RUNNER_DIR" >&2
  exit 1
fi

if ! id "$RUNNER_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$RUNNER_DIR" --shell /bin/bash "$RUNNER_USER"
fi
usermod --append --groups docker "$RUNNER_USER"

mkdir -p "$RUNNER_DIR" /srv/lightwalletd/snapshots
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR" /srv/lightwalletd/snapshots

ARCHIVE="/tmp/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
curl --fail --location --retry 3 \
  --output "$ARCHIVE" \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
printf '%s  %s\n' "$RUNNER_SHA256" "$ARCHIVE" | sha256sum --check --status
tar --extract --gzip --file "$ARCHIVE" --directory "$RUNNER_DIR"
rm -f "$ARCHIVE"
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"

if command -v apt-get >/dev/null 2>&1; then
  "$RUNNER_DIR/bin/installdependencies.sh"
elif command -v dnf >/dev/null 2>&1; then
  # Amazon Linux 2023 is Fedora-compatible but is not recognized by the
  # runner's distro detection.
  dnf install -y lttng-ust openssl-libs krb5-libs zlib libicu
else
  echo "error: supported package manager not found (apt-get or dnf)" >&2
  exit 1
fi

(
  cd "$RUNNER_DIR"
  sudo -u "$RUNNER_USER" \
    ./config.sh \
    --unattended \
    --url "$RUNNER_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work _work \
    --replace
)
unset RUNNER_TOKEN

(
  cd "$RUNNER_DIR"
  ./svc.sh install "$RUNNER_USER"
  ./svc.sh start
  ./svc.sh status
)
