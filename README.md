# lightwalletd snapshots

A two-command prototype for creating a snapshot from a synced Docker-based `lightwalletd` and starting another `lightwalletd` from that snapshot.

The current scripts are fixed to Zcash mainnet and default to
`electriccoinco/lightwalletd:v0.5.4`.

For the complete data flow, consistency model, Docker restore mechanics, validation evidence, failure behavior, and security limitations, see [How lightwalletd snapshots work](docs/how-snapshots-work.md).

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
  https://example.com/lightwalletd-mainnet-v0.5.4-TIMESTAMP.tar.zst
```

From a local file:

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh \
  ./lightwalletd-mainnet-v0.5.4-TIMESTAMP.tar.zst
```

Defaults:

```text
CONTAINER_NAME=lightwalletd
VOLUME_NAME=lightwalletd-data
IMAGE=electriccoinco/lightwalletd:v0.5.4
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

See [`PLAN.md`](PLAN.md) for scope and acceptance criteria.

## Automated daily publication to Amazon S3

The repository includes:

- [`infra/snapshot-storage.yaml`](infra/snapshot-storage.yaml), which creates a
  private, encrypted, versioned S3 bucket with retention controls and attaches
  prefix-scoped publication permissions through its bucket policy without
  modifying the Terraform-managed source EC2 role.
- [`scripts/publish-to-s3.sh`](scripts/publish-to-s3.sh), which uploads a
  snapshot and its sidecars before atomically publishing `latest.json`.
- [`.github/workflows/daily-snapshot.yml`](.github/workflows/daily-snapshot.yml),
  which runs daily on the dedicated source-host GitHub Actions runner.

The restore command also accepts an authenticated S3 URI:

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh \
  s3://BUCKET/mainnet/v0.5.4/snapshots/TIMESTAMP/snapshot.tar.zst
```

S3 remains private. Publishing and direct S3 restores require an instance role
or another short-lived identity authorized for the snapshot key prefix. Public
downloads are served only through the stack's CloudFront distribution.

Discover and download the latest public snapshot with resumable transfers:

```bash
BASE_URL="https://d2t6ule2qr31fe.cloudfront.net"
PREFIX="mainnet/v0.5.4"

curl -fsSL "$BASE_URL/$PREFIX/latest.json" -o latest.json
SNAPSHOT_KEY="$(jq -r '.snapshot_key' latest.json)"
CHECKSUM_KEY="$(jq -r '.checksum_key' latest.json)"
EXPECTED_SHA256="$(jq -r '.sha256' latest.json)"

curl -fL --continue-at - "$BASE_URL/$SNAPSHOT_KEY" -o snapshot.tar.zst
curl -fsSL "$BASE_URL/$CHECKSUM_KEY" -o snapshot.tar.zst.sha256
SIDECAR_SHA256="$(awk 'NR == 1 {print $1}' snapshot.tar.zst.sha256)"
test "$SIDECAR_SHA256" = "$EXPECTED_SHA256"
printf '%s  snapshot.tar.zst\n' "$EXPECTED_SHA256" | sha256sum --check
```

The deployment also exposes this host as the `SnapshotDownloadDomain`
CloudFormation output. Timestamped archives are immutable and cacheable;
`latest.json` is deliberately not cached.

The restore script accepts an archive URL directly and automatically retrieves
its checksum sidecar:

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zakura_default \
  ./scripts/start-from-snapshot.sh \
  "https://d2t6ule2qr31fe.cloudfront.net/$SNAPSHOT_KEY"
```
