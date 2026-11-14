# Zovereign

Run your own Zcash mainnet infrastructure in **one Docker container**: an
archive Zakura node and lightwalletd, both bootstrapped from published snapshots.
lightwalletd connects to the RPC on your bundled Zakura node.

## Quick start

Install Docker with the Compose plugin, then run:

```bash
git clone https://github.com/zcashlabs/zovereign.git
cd zovereign
docker compose up -d --build
```

No account, RPC credentials, or separate node is needed. The first start
downloads and verifies both snapshots, restores the databases, catches Zakura
up to the network, and starts lightwalletd. Later starts reuse the data;
interrupted downloads resume.

**Plan for SSD storage, 4 CPU cores, 16 GiB RAM, and a 1 TB data disk.** The
initial download is approximately 300 GB as of November 2026, with additional
space required during extraction. First startup can take hours.

Linux amd64 is the tested platform. Docker Desktop users must allocate these
resources to Docker's VM. On Windows, use Linux containers and run the commands
in WSL 2. Apple Silicon uses amd64 emulation; native arm64 is not currently
supported, and a full mainnet restore under emulation has not been validated.

## Run the published image directly

Without cloning the repository or building an image, run:

```bash
docker run -d --platform linux/amd64 \
  --name zovereign --restart unless-stopped --stop-timeout 120 \
  --log-driver json-file --log-opt max-size=20m --log-opt max-file=3 \
  --mount type=volume,src=zovereign-data,dst=/data \
  -p 8233:8233 -p 127.0.0.1:9067:9067 \
  ghcr.io/zcashlabs/zovereign:latest
```

The image is publicly pullable without a registry login. Choose this command
or Compose for a deployment; both use the same container and volume names.
Use a `sha-<full-commit>` image tag or digest to pin a specific tested build.

## Check readiness and connect

```bash
docker logs -f --tail=100 zovereign
docker inspect --format '{{.State.Health.Status}}' zovereign
```

Health remains `starting` during bootstrap. `healthy` means the node is near
the network tip and lightwalletd can serve an actual compact block through it.

Point a local wallet or gRPC client that supports a custom plaintext server at
`127.0.0.1:9067`. The default gRPC port is bound to your machine's loopback
interface. Port `8233` accepts Zcash peers; node RPC stays private inside the
container. For remote wallets, use a trusted tunnel or configure TLS using
[the deployment guide](docs/single-container.md#serving-remote-clients-with-tls).

The container includes grpcurl, so you can also query it directly:

```bash
docker exec zovereign grpcurl -plaintext 127.0.0.1:9067 \
  cash.z.wallet.sdk.rpc.CompactTxStreamer/GetLightdInfo
```

## Stop, resume, and update

For a Compose deployment:

```bash
docker compose stop          # Gracefully stop both services; keep data.
docker compose start         # Resume using the same databases.
```

To update the source and recreate the container:

```bash
git pull --ff-only
docker compose up -d --build
```

The persistent volume is named `zovereign-data`. Keep it across updates.
`docker compose down` also keeps it; **`docker compose down -v` permanently
deletes the databases and download progress**. Check release notes before
updating database versions; an incompatible database is refused rather than
silently replaced.

With the direct image command, stop and resume using `docker stop -t 120
zovereign` and `docker start zovereign`. For image updates, follow
[the guide](docs/single-container.md#updating-a-published-image).

## Configure and learn more

The defaults work without a configuration file. Optionally copy `.env.example`
to `.env` to change host ports, download concurrency, or the disk reserve, then
run `docker compose up -d --build` to apply changes.

- [Deployment guide](docs/single-container.md): plain Docker commands, storage,
  recovery, configuration, TLS, and full-mainnet validation.
- [Contributing](CONTRIBUTING.md): build and run the small fixture tests without
  downloading the blockchain, plus image publication instructions.
- [Agent instructions](AGENTS.md): repository map and implementation invariants.
- [Snapshot creation and publishing](docs/snapshot-publishing.md): maintain the
  publisher or restore standalone lightwalletd against an existing RPC server.

This image bundles Zakura **1.8.7** (archive database major **30**) and
lightwalletd **v0.6.0**, with upstream images pinned by digest. It passed 16
bootstrap and lifecycle tests and a complete mainnet restore on a clean Linux
VPS on 14 November 2026. lightwalletd loaded **3,571,794 cached blocks**, served
blocks near the bundled node's tip, and restarted without downloading again.

Snapshots skip the initial historical sync, so their imported history depends
on the publishers. SHA-256 verifies download integrity; it does not authenticate
the publisher. See the guide's
[trust boundary](docs/single-container.md#trust-and-validation-boundary).
