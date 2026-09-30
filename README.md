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

The source container is stopped while its database is archived to guarantee consistency. A cleanup trap restarts it if creation succeeds, fails, or is interrupted.

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

S3 remains private. The AWS CLI on the publishing and restore hosts must use an
instance role or another short-lived identity authorized for the snapshot key
prefix.
