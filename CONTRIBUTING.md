# Contributing to Zovereign

Zovereign runs a snapshot-restored mainnet Zakura node and lightwalletd in one
container. The [deployment guide](docs/single-container.md) describes startup,
on-disk state, failure recovery, and the trust boundary. Read [AGENTS.md](AGENTS.md)
before changing the runtime.

## Build and test without a blockchain download

Use a Linux amd64 Docker host. From the repository root:

```bash
docker compose config --quiet
docker build --platform linux/amd64 -t zovereign:test .
docker run --rm --entrypoint python3 \
  -e STACK_MODULE=/opt/zovereign/stack.py \
  --mount "type=bind,src=$(pwd)/tests,dst=/tests,readonly" \
  zovereign:test /tests/test_stack.py
```

These 16 tests use tiny local HTTP snapshots and temporary databases. They
exercise retries, resumption, checksum and compatibility rejection, unsafe
archives, disk exhaustion, process supervision, and application health. They
also run the bundled Zakura and lightwalletd binaries briefly to catch
configuration and logging regressions. They do not download mainnet data and
do not mount your deployment's volume. Run them inside the image as shown:
the runtime's Python version, zstd, curl, and binaries are part of the contract.

`docker compose up` starts a real mainnet bootstrap. Use the fixture command
above for ordinary development, and never use `down -v` against someone else's
deployment. An integration test or an upgrade should use its own volume.

For full-mainnet acceptance, provision the capacity in the deployment guide,
start the stack with a fresh volume, and run on that Docker host:

```bash
python3 tests/accept_mainnet.py --container zovereign --restart \
  --output /absolute/path/to/mainnet-validation.json
```

This is an opt-in check that waits for readiness and restarts the selected
container to prove database reuse. It can take hours and download hundreds of
gigabytes. Acceptance reports can contain deployment details; keep them out
of commits unless reviewed and sanitized.

## Continuous integration and image publication

[The container workflow](.github/workflows/container-test.yml) validates
Compose, builds the Linux amd64 image, and runs the fixture and real-binary
tests on GitHub-hosted runners. Pull requests run tests without publishing.
Relevant pushes to `main`, or a manual run on `main`, publish the tested image
to `ghcr.io/zcashlabs/zovereign` as `latest` and `sha-<full-commit>` using the
workflow's `GITHUB_TOKEN`. Forks publish under their own repository namespace.
No registry password is required in repository secrets.

The project's image has been verified with an unauthenticated pull. For new
packages or forks, check visibility: if GitHub creates a private package, an
administrator must set it to **Public** in GitHub package settings before
anonymous pulls work. Confirm an unauthenticated pull on a clean Docker host
before advertising a prebuilt image. The source quick start works
independently of registry visibility. See
[GitHub's registry guide](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

Use immutable `sha-<full-commit>` tags or image digests for controlled upgrades.
Changing an environment variable does not upgrade a bundled binary or migrate
an incompatible database. Update upstream version tags and digests together,
review manifest compatibility checks, run tests, and document validation.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Dockerfile`, `.dockerignore` | Pinned combined runtime; minimal build context. |
| `compose.yaml`, `.env.example` | Local deployment and optional host settings. |
| `docker/stack.py` | Bootstrap, validation, supervision, and health checks. |
| `tests/test_stack.py` | Small offline fixtures and bundled-binary regressions. |
| `tests/accept_mainnet.py` | Opt-in check of a real running mainnet deployment. |
| `scripts/` | Standalone snapshot creation, restore, and publication. |
| `deploy/digitalocean/`, `infra/digitalocean/` | Source publisher deployment. |
| `infra/snapshot-storage.yaml`, `PLAN.md` | Earlier AWS deployment and original plan. |

Keep runtime changes scoped and describe what was tested in your commit or PR.
Documentation should distinguish fixture coverage from a real mainnet restore.
