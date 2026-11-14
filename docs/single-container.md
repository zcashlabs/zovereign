# One container for a snapshot-bootstrapped Zcash node and lightwalletd

The root `Dockerfile` packages Zakura 1.8.7 and lightwalletd v0.6.0 into one
Linux amd64/arm64 image. It restores the latest mainnet archive from the Zakura
publisher and the latest compatible compact-block cache from our publisher,
then runs both services. lightwalletd uses the Zakura RPC inside the same
container at `127.0.0.1:8232`.

No separate full node, Docker socket, or RPC credentials are required. The
plain Docker commands below do not require Compose. The image contains the binaries and bootstrap tools;
the large snapshots download at first startup, rather than being baked into
the image.

## Build and run

The [README quick start](../README.md#quick-start) uses `docker compose up -d
--build` from a clean checkout. Compose creates the same single container with
bounded log rotation, a two-minute shutdown grace period, and a persistent
`zovereign-data` volume. Copy `.env.example` to `.env` to adjust its optional
host settings. `.env` is read by Compose; plain `docker run` uses explicit `-e`
arguments instead.

From the repository root, build the image once:

```bash
docker build -t zovereign:local .
```

Start the complete stack with one command:

```bash
docker run -d \
  --name zovereign \
  --restart unless-stopped \
  --stop-timeout 120 \
  --log-driver json-file --log-opt max-size=20m --log-opt max-file=3 \
  --mount type=volume,src=zovereign-data,dst=/data \
  -p 8233:8233 \
  -p 127.0.0.1:9067:9067 \
  zovereign:local
```

Docker creates the named volume automatically. Keep it when replacing or
updating the container. Databases and unfinished downloads persist there.
Run one stack per volume; the entrypoint locks the volume to prevent two
containers from opening the same database.

Port `8233` accepts mainnet peer connections. Port `9067` serves lightwalletd
gRPC on the host's loopback interface. For another machine, use an SSH tunnel,
or configure TLS as shown below. RPC port `8232` and the lightwalletd HTTP
listener stay on the container's loopback interface.

## Updating a published image

The [README](../README.md#run-the-published-image-directly) also provides a
prebuilt image command. To replace that container while keeping the databases:

```bash
docker pull ghcr.io/zcashlabs/zovereign:latest
docker stop -t 120 zovereign
docker rm zovereign
```

Run the README's published-image command again with the same `zovereign-data`
volume. Read the version's compatibility notes before upgrading; use an
immutable commit tag or digest when you need a controlled deployment. Removing
the container does not remove this named volume. If you customized bindings,
TLS mounts, or environment variables, preserve those options when recreating it.

## Capacity and first startup

Use a Linux amd64 or arm64 host with SSD storage. Start with 4 CPU cores, 16 GiB RAM,
and a dedicated 1 TB data volume; workload and future chain growth can require
more. On Docker Desktop, these resources must be allocated to Docker's VM.
Use Linux containers and WSL 2 on Windows. Docker and Compose select the native
architecture automatically; Apple Silicon uses the ARM64 image without amd64
emulation. The images run the Linux services inside Docker's VM on macOS and
Windows, rather than building native services for those operating systems.

CI runs fixture and bundled-binary tests natively on both Linux architectures.
A full mainnet restore has only been validated on Linux amd64. ARM64 and
Docker Desktop full-mainnet acceptance is still pending. The pinned Zakura
image does not support other CPU architectures.

On 14 November 2026, the published Zakura archive was about 275 GB compressed,
and the lightwalletd archive was about 24.6 GB. Initial bootstrap downloads
both archives and extracts them in sequence. During each extraction, the
compressed archive and the restored database coexist. The compressed archive
is deleted after that database is successfully installed. Allow substantial
free space for extraction, RocksDB maintenance, and chain growth.

The entrypoint keeps a 20 GiB free-space reserve by default. It checks available
space before downloading and before extracting each file. These checks do not
reserve space against other processes using the same filesystem.

First startup can take hours depending on bandwidth, SSD throughput, and node
catch-up. It avoids rebuilding all historical state from network blocks, but
still downloads the complete snapshots and processes blocks after their tips.

## Startup sequence and recovery

1. Initialize `/data`, acquire an exclusive volume lock, and drop to UID/GID
   `10001`. Bootstrap, Zakura, and lightwalletd run without root privileges.
2. Fetch the latest mainnet **archive** entry from the Zakura manifest. Refuse
   snapshots whose database major differs from the image's supported major
   `30`.
3. Pin the selected manifest in `/data/bootstrap/zakura.json`. Download using
   256 MiB HTTP ranges with three parallel connections and retries, checking
   returned range offsets and sizes. Any finished range frees a connection for
   more work; active and completed ranges are bounded to twice the worker count.
   A slow early range can still apply backpressure when that window fills.
   Ranges commit to the archive in order.
   Completed bytes persist in a `.part` file. Restarting continues that same
   snapshot, even if the publisher's `latest` entry has changed.
4. Verify the entire compressed archive's SHA-256, then extract into a staging
   directory. Accept only regular files and directories under `state/`; reject
   traversal, links, and special files. Require the expected mainnet database.
5. Write provenance to `.bootstrap.json`, rename staging to `/data/zakura`,
   and remove the compressed archive.
6. Repeat for the lightwalletd snapshot, requiring mainnet, manifest format
   version `1`, the bundled lightwalletd version, and `db/main/{blocks,lengths}`.
7. Start Zakura in mainnet archive mode. Wait for a working RPC response whose
   height is within five blocks of its estimated network tip.
8. Start lightwalletd against loopback RPC. Its historical compact-block cache
   is already populated; it catches up from the restored snapshot.
9. Keep both processes supervised. If either exits, stop its peer and exit with
   an error so Docker's restart policy can restart the complete stack.

An existing completed bootstrap is reused without contacting either snapshot
publisher. Changing a manifest URL does not replace an installed database.
The entrypoint refuses to overwrite a nonempty database directory without its
bootstrap marker. Use a separate volume for independent deployments or tests.

Interrupted downloads resume. Interrupted extraction is retried from the
retained verified archive. A checksum mismatch discards the corrupt archive.
An extraction error removes only the staging directory. Existing installed
databases are preserved. If a pinned archive has expired on the publisher,
stop the container and clear only that service's unfinished files under
`/data/bootstrap/` to select a fresh archive; preserve installed databases.

Stop gracefully with `docker stop -t 120 zovereign`. Both services receive
SIGTERM, allowing their databases to flush. An external SIGKILL or power loss
cannot guarantee graceful flushing; the next startup relies on the databases'
normal recovery.

## Logs and application health

```bash
docker logs -f zovereign
docker inspect --format '{{.State.Health.Status}}' zovereign
docker exec zovereign python3 /opt/zovereign/stack.py health
```

Docker health remains `starting` during the initial bootstrap, with a 24-hour
start period. The probe can report `healthy` earlier as soon as it succeeds.
It checks that Zakura serves mainnet in archive mode and is close to its
estimated tip, calls lightwalletd's `GetLightdInfo`, and requests an actual
compact block one block behind the node tip with `GetBlock`. An open TCP port
alone is insufficient. Docker's standalone restart policy responds to process
exit, not to an `unhealthy` status.

To test the frontend directly from the host with grpcurl installed:

```bash
grpcurl -plaintext 127.0.0.1:9067 \
  cash.z.wallet.sdk.rpc.CompactTxStreamer/GetLightdInfo
```

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `/data` | Root of the persistent volume; mount your volume at the same path if changed. |
| `ZAKURA_SNAPSHOT_MANIFEST` | `https://zakura.valargroup.dev/mainnet/snapshots.json` | Zakura snapshot catalog. |
| `LIGHTWALLETD_SNAPSHOT_MANIFEST` | `https://165-232-183-161.sslip.io/latest.json` | Our lightwalletd manifest. |
| `DOWNLOAD_CHUNK_MIB` | `256` | Size of each retryable HTTP range. |
| `DOWNLOAD_WORKERS` | `3` | Parallel range requests, from 1 to 8. Completed ranges commit in order; uncommitted ranges are discarded on shutdown. |
| `DISK_RESERVE_GIB` | `20` | Free space retained during bootstrap. |
| `RPC_READY_TIMEOUT` | `86400` | Seconds allowed for Zakura RPC startup and catch-up. |
| `TLS_CERT`, `TLS_KEY` | unset | Paths to a readable PEM certificate and private key inside the container. Set both. |

The Dockerfile pins both upstream images and grpcurl by digest. Mainnet,
loopback RPC, archive mode, and the database paths are set by the supervisor.
The image's version and database-major environment values describe its bundled
binaries; changing those values does not upgrade the binaries or convert data.

If using a bind mount instead of a named volume, its parent directories must
be traversable by UID `10001`. The initial root setup adjusts ownership of the
data root and bootstrap directory only; it does not recursively change
ownership of unrelated host files.

## Serving remote clients with TLS

Provide a certificate for your hostname and a key readable by UID `10001`:

```bash
docker run -d \
  --name zovereign \
  --restart unless-stopped \
  --stop-timeout 120 \
  --mount type=volume,src=zovereign-data,dst=/data \
  --mount type=bind,src=/absolute/path/to/tls,dst=/tls,readonly \
  -e TLS_CERT=/tls/fullchain.pem \
  -e TLS_KEY=/tls/privkey.pem \
  -p 8233:8233 \
  -p 9067:9067 \
  zovereign:local
```

The certificate and key must already exist; this image does not provision or
renew them. Without them, gRPC uses plaintext, suitable for the loopback-bound
default or a trusted tunnel. The internal TLS health probe skips certificate
hostname verification on loopback; wallet clients must verify the public
server certificate normally.

## Trust and validation boundary

The snapshots provide historical node state and compact-block data. You still
trust their publishers for that imported history. SHA-256 catches corruption;
it does not authenticate a publisher that can replace both archive and
manifest. Running your own node subsequently removes the need for a remote
RPC backend, but does not turn imported snapshot history into a from-genesis
independent verification.

On 14 November 2026, the image was built and tested on a clean Linux amd64 VPS
with 12 CPU threads, 32 GiB RAM, and a 1 TB SSD. All 16 tests passed inside the
runtime image. They cover download/resume, checksum and compatibility rejection,
safe extraction, persistence, process startup order, private RPC configuration,
privilege dropping, graceful shutdown, and paired failure recovery. Real-binary
regressions also check Zakura's configuration environment and lightwalletd's
logging after privilege dropping; lifecycle tests otherwise use small synthetic
databases and substitute services.

The separate full-mainnet acceptance test passed on that VPS:

- Downloaded and verified the 275,461,001,203-byte Zakura archive and
  24,580,470,326-byte lightwalletd archive from their published manifests.
- Restored both databases and removed the compressed archives and range files.
- Loaded 3,571,794 blocks from lightwalletd's imported cache.
- Reached Zakura's live tip and served an actual compact block through gRPC.
- Restarted the container, reused both databases without downloads, and returned
  to `healthy` with no unexpected automatic restarts in the fixed image.
- Served `GetLightdInfo` and a compact block to a client on another Tailscale
  machine, using the VPS's Tailscale-bound gRPC port.

The tested deployment used `DOWNLOAD_WORKERS=8` for the distant lightwalletd
publisher. It published gRPC on localhost and the VPS's Tailscale IP, retaining
loopback-only RPC inside the container. Docker log rotation was bounded to
three 20 MiB files. Build caches and superseded images were pruned after builds.

Raw acceptance reports stay on the development host because they can contain
deployment details. To reproduce the checks, use the commands below and keep
your report outside the repository or in the ignored `output/` directory.

Run the same tests inside the actual runtime image:

```bash
docker run --rm --entrypoint python3 \
  -e STACK_MODULE=/opt/zovereign/stack.py \
  --mount type=bind,src="$(pwd)/tests",dst=/tests,readonly \
  zovereign:local /tests/test_stack.py
```

For full acceptance on a clean volume, require `healthy`, confirm logs report
loading the restored lightwalletd cache, compare both services' tips, call
`GetBlock` and `GetLightdInfo`, restart the container, and confirm neither
snapshot downloads again. Never use the production publisher's data volume
for this test.

On the Docker host, run the opt-in real-mainnet acceptance check after starting
the container:

```bash
python3 tests/accept_mainnet.py --container zovereign --restart \
  --output /absolute/path/to/mainnet-validation.json
```

It waits for application readiness, checks the imported snapshot height and
temporary archive cleanup, captures cache-load logs, and verifies database reuse
after restarting the container. This test does not run in fixture CI.
