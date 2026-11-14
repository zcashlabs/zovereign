# Snapshot creation and publishing

Run the commands below from the repository root. For the complete combined
node and wallet server, see the [quick start](../README.md).

## Snapshot creation and standalone lightwalletd restore

The scripts below are also available independently. The restore script
requires an existing synchronized Zakura/Zcash RPC server; the combined image
in the [quick start](../README.md) runs its own.

For the combined image's startup sequence, recovery behavior, validation, and
trust boundary, see [the deployment guide](single-container.md).

## Requirements

- Bash
- Docker
- Zstandard (`zstd`)
- `sha256sum`
- `curl` on the restore host when using an HTTP URL
- A synchronized Zakura/Zcash node reachable from the restored container

## 1. Create a snapshot

Run this on the host containing the synced `lightwalletd` container:

```bash
sudo ./scripts/create-snapshot.sh ./snapshots
```

The default source container is `lightwalletd`, and the default database path is `/var/lib/lightwalletd`. Override them when necessary:

```bash
sudo \
  CONTAINER_NAME=lightwalletd \
  DATA_DIR=/var/lib/lightwalletd \
  ./scripts/create-snapshot.sh ./snapshots
```

The source container is quiesced while its database is archived to guarantee
consistency. Persistent containers are stopped; auto-remove containers are
paused so Docker does not delete them. A cleanup trap resumes the source if
creation succeeds, fails, or is interrupted.

The command creates:

```text
snapshot.tar.zst
snapshot.tar.zst.sha256
snapshot.tar.zst.info
```

Upload these files to an ordinary HTTP server or copy them directly to the restore host.

## 2. Restore and start lightwalletd

From an HTTPS URL:

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PORT=8232 \
  RPC_USER=lightwalletd \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh \
  https://example.com/lightwalletd-mainnet-v0.6.0-TIMESTAMP.tar.zst
```

From a local file:

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh \
  ./lightwalletd-mainnet-v0.6.0-TIMESTAMP.tar.zst
```

Defaults:

```text
CONTAINER_NAME=lightwalletd
VOLUME_NAME=lightwalletd-data
IMAGE=electriccoinco/lightwalletd:v0.6.0
DATA_DIR=/var/lib/lightwalletd
GRPC_PORT=9067
RPC_PORT=8232
RPC_USER=lightwalletd
START_TIMEOUT=60
```

`RPC_HOST` and `RPC_PASSWORD` are required. `DOCKER_NETWORK` is required when `RPC_HOST` is the name of another Docker container.

The restore command refuses to overwrite an existing container or volume. For an isolated test, choose different names and a different host port:

```bash
sudo \
  CONTAINER_NAME=lightwalletd-snapshot-test \
  VOLUME_NAME=lightwalletd-snapshot-test-data \
  GRPC_PORT=19067 \
  RPC_HOST=zakura \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh ./snapshot.tar.zst
```

`FORCE=1` removes an existing target container and volume permanently. Do not use it against a deployment containing data you need.

## Security limitations

The checksum detects accidental corruption but does not authenticate the publisher. Only use snapshots obtained from a source you trust. Signed manifests remain deferred.

## Automated daily publication on DigitalOcean

[The daily workflow](../.github/workflows/daily-snapshot.yml) runs on the source
host's self-hosted runner. It creates a v0.6.0 snapshot and calls
[`publish-local.sh`](../scripts/publish-local.sh), which validates the checksum,
atomically publishes `latest.json`, and retains three archives. Caddy serves
the snapshot directory over HTTPS directly from the DigitalOcean volume.

Discover and download the latest public snapshot with resumable transfers:

```bash
BASE_URL="https://165-232-183-161.sslip.io"

curl -fsSL "$BASE_URL/latest.json" -o latest.json
SNAPSHOT_URL="$(jq -r '.snapshot_url' latest.json)"
CHECKSUM_URL="$(jq -r '.checksum_url' latest.json)"
EXPECTED_SHA256="$(jq -r '.sha256' latest.json)"

curl -fL --continue-at - "$SNAPSHOT_URL" -o snapshot.tar.zst
curl -fsSL "$CHECKSUM_URL" -o snapshot.tar.zst.sha256
SIDECAR_SHA256="$(awk 'NR == 1 {print $1}' snapshot.tar.zst.sha256)"
test "$SIDECAR_SHA256" = "$EXPECTED_SHA256"
printf '%s  snapshot.tar.zst\n' "$EXPECTED_SHA256" | sha256sum --check
```

Timestamped archives are immutable and cacheable; `latest.json` is deliberately
not cached. These manual download commands are resumable; the standalone
restore script's own temporary HTTP downloads restart from zero. The combined
image has persistent resumable downloads built in.

The restore script accepts an archive URL directly and automatically retrieves
its checksum sidecar:

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh \
  "$SNAPSHOT_URL"
```
