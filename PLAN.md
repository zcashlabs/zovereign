# Lightwalletd Snapshots — v0 Prototype Plan

## Goal

Prove the complete idea with two shell commands:

```bash
# On a server that already has a fully synced lightwalletd container
./scripts/create-snapshot.sh

# On a new server with access to a synced Zakura/Zcash node
./scripts/start-from-snapshot.sh https://example.com/lightwalletd-snapshot.tar.zst
```

The first command creates a compressed copy of the synced `lightwalletd` database. The second downloads that copy, restores it into a Docker volume, and starts a new `lightwalletd` container that only needs to catch up from the snapshot height.

This prototype is deliberately small. It is not yet a hosted snapshot service, scheduler, API, CDN, mirror network, or production-grade CLI.

## Fixed v0 assumptions

To avoid premature compatibility work, v0 supports one known configuration:

- Docker is installed.
- Zcash mainnet only.
- `electriccoinco/lightwalletd:v0.5.4`.
- Source container name: `lightwalletd` by default.
- Database path inside the container: `/var/lib/lightwalletd`.
- Output format: `tar.zst`.
- The destination already has access to a synchronized Zakura/Zcash RPC server.
- The user supplies the RPC host, port, username, password, and Docker network when restoring.

Container names and paths can be overridden through environment variables, but v0 does not attempt to support every deployment layout automatically.

## Deliverables

```text
lightwalletd-snapshots/
  README.md
  PLAN.md
  scripts/
    create-snapshot.sh
    start-from-snapshot.sh
```

Optional small files created beside each snapshot:

```text
lightwalletd-mainnet-v0.5.4-20260822T170614Z.tar.zst
lightwalletd-mainnet-v0.5.4-20260822T170614Z.tar.zst.sha256
lightwalletd-mainnet-v0.5.4-20260822T170614Z.tar.zst.info
```

## Command 1: Create a snapshot

### Usage

```bash
sudo ./scripts/create-snapshot.sh [output-directory]
```

Example:

```bash
sudo CONTAINER_NAME=lightwalletd \
  ./scripts/create-snapshot.sh ./snapshots
```

### Workflow

1. Verify required commands exist: `docker`, `zstd`, `sha256sum`, and `date`.
2. Confirm the source container exists and is running.
3. Confirm its configured image is `electriccoinco/lightwalletd:v0.5.4`.
4. Print the latest relevant `lightwalletd` log line so the operator can see its current block.
5. Install a shell `trap` that restarts the source container on failure or interruption.
6. Stop the `lightwalletd` container so the database cannot change while it is copied.
7. Stream `/var/lib/lightwalletd` from the stopped container with `docker cp` and compress it with Zstandard.
8. Restart the source container immediately after the archive is complete.
9. Confirm the container is running again.
10. Generate a SHA-256 sidecar file.
11. Write a small human-readable `.info` file containing:
    - Creation time in UTC
    - Source image
    - Source container name
    - Last observed block from the logs
    - Archive filename and size
12. Print the snapshot path and checksum.

Conceptually, the core operation is:

```bash
docker stop lightwalletd
docker cp lightwalletd:/var/lib/lightwalletd/. - | zstd -T0 -3 -o snapshot.tar.zst
docker start lightwalletd
sha256sum snapshot.tar.zst > snapshot.tar.zst.sha256
```

The actual script must use a cleanup trap; the simplified example above is not safe enough by itself.

### Why stop the container?

Copying the database while `lightwalletd` is writing could create a corrupt or internally inconsistent snapshot. For v0, simplicity and correctness are more important than minimizing downtime, so `lightwalletd` remains stopped while the archive is streamed.

If this proves too slow, v1 can introduce a two-pass copy or filesystem snapshots. That optimization is intentionally excluded from v0.

## Making the snapshot downloadable

Publication is manual in v0. The operator can copy the three generated files to any ordinary HTTP server, object bucket, GitHub release, or other download location.

Example with a generic server:

```bash
scp snapshots/lightwalletd-mainnet-v0.5.4-* user@download-server:/var/www/snapshots/
```

The scripts do not manage uploads, retention, indexes, signing keys, or CDN configuration in v0.

## Command 2: Restore and start from a snapshot

### Usage

```bash
sudo \
  RPC_HOST=zakura \
  RPC_PORT=8232 \
  RPC_USER=lightwalletd \
  RPC_PASSWORD='replace-me' \
  DOCKER_NETWORK=zcash \
  ./scripts/start-from-snapshot.sh \
  https://example.com/lightwalletd-mainnet-v0.5.4-20260822T170614Z.tar.zst
```

A local archive path should also be accepted:

```bash
sudo RPC_HOST=zakura RPC_PASSWORD='replace-me' \
  ./scripts/start-from-snapshot.sh ./lightwalletd-snapshot.tar.zst
```

### Configuration defaults

```text
CONTAINER_NAME=lightwalletd
IMAGE=electriccoinco/lightwalletd:v0.5.4
VOLUME_NAME=lightwalletd-data
GRPC_PORT=9067
RPC_PORT=8232
RPC_USER=lightwalletd
DATA_DIR=/var/lib/lightwalletd
```

`RPC_HOST` and `RPC_PASSWORD` should be required. `DOCKER_NETWORK` is required when the RPC hostname is another Docker container name; it may be omitted when `RPC_HOST` is otherwise reachable from the container.

### Workflow

1. Verify required commands exist: `docker`, `curl`, `zstd`, `sha256sum`, and `tar`.
2. Refuse to overwrite an existing container or Docker volume unless `FORCE=1` is explicitly set.
3. Download the snapshot to a temporary file using `curl --fail --location`.
4. If the adjacent `.sha256` URL/file exists, download it and verify the archive.
5. Create a new Docker volume.
6. Use a temporary helper container to extract the archive into the volume.
7. Confirm the restored volume contains the expected `db/` directory.
8. Start `electriccoinco/lightwalletd:v0.5.4` with:
    - The restored volume mounted at `/var/lib/lightwalletd`
    - gRPC port `9067` published
    - The supplied backing-node RPC settings
    - The supplied Docker network, if any
9. Wait for the container to remain running and for TCP port `9067` to become reachable.
10. Print recent logs, including the first observed `Waiting for block` line.
11. Print `docker logs -f lightwalletd` as the command the operator can use to watch catch-up.
12. Remove temporary downloaded and extraction files.

The resulting container command will be equivalent to:

```bash
docker run -d \
  --name lightwalletd \
  --restart unless-stopped \
  --network zcash \
  -p 9067:9067 \
  -v lightwalletd-data:/var/lib/lightwalletd \
  electriccoinco/lightwalletd:v0.5.4 \
  --no-tls-very-insecure \
  --grpc-bind-addr=0.0.0.0:9067 \
  --rpchost=zakura \
  --rpcport=8232 \
  --rpcuser=lightwalletd \
  --rpcpassword=replace-me \
  --data-dir=/var/lib/lightwalletd \
  --log-file=/dev/stdout \
  --log-level=6
```

The script must build this command safely as an argument list rather than evaluating a generated shell string.

## Minimum safeguards retained in v0

Even a prototype should keep these safeguards:

- Stop the source during archive creation.
- Always restart the source through a cleanup trap.
- Write to a temporary archive and rename it only after successful compression.
- Produce and verify SHA-256 when the sidecar is available.
- Do not silently overwrite an existing destination database.
- Extract only into a new Docker volume.
- Require the same hardcoded `lightwalletd` version used to create the snapshot.
- Delete a newly created volume if extraction fails.
- Never include RPC credentials or other configuration in the snapshot.

Publisher authentication and signed manifests are deferred. Therefore, users must only download v0 snapshots from a source they trust; SHA-256 alone does not protect against a malicious download server.

## Prototype acceptance test

The v0 experiment succeeds when the following can be demonstrated:

1. Start with the currently synced `lightwalletd` container.
2. Run `create-snapshot.sh` and obtain a non-empty archive and matching checksum.
3. Confirm the original container restarts and continues waiting for new blocks.
4. Move the archive to a clean machine or clean Docker environment.
5. Run `start-from-snapshot.sh` against a synchronized Zakura/Zcash node.
6. Confirm the new `lightwalletd` starts without database errors.
7. Confirm its first requested block is near the snapshot tip rather than genesis.
8. Confirm it catches up to and waits at the backing node's current tip.
9. Compare total restore/catch-up time with a from-genesis sync.

## Explicitly deferred until after v0

- Scheduled snapshots
- Automatic uploads
- S3/CDN integration
- Signed manifests
- Snapshot catalogs and `latest.json`
- Multiple versions or networks
- Cross-version database compatibility
- Automated disposable-container validation
- Metrics and alerting
- Retention policies
- Mirrors
- Delta snapshots
- Web UI or API
- Minimal-downtime filesystem snapshotting

## Implementation order

1. Implement `create-snapshot.sh`.
2. Test it against the existing synchronized server and measure archive size and source downtime.
3. Implement `start-from-snapshot.sh`.
4. Test restore into a separate container and volume on the same server.
5. Test download and restore from a clean second environment.
6. Document the exact two commands and required environment variables in `README.md`.

That is the complete v0 scope.
