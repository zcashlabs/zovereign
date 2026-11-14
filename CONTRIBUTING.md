# Contributing to Zovereign

Zovereign runs a snapshot-restored mainnet Zakura node and lightwalletd in one
container. The [deployment guide](docs/single-container.md) describes startup,
on-disk state, failure recovery, and the trust boundary. Read [AGENTS.md](AGENTS.md)
before changing the runtime.

## Build and test without a blockchain download

Use a Linux amd64 or arm64 Docker host. From the repository root:

```bash
docker compose config --quiet
docker build -t zovereign:test .
docker run --rm --entrypoint python3 \
  -e STACK_MODULE=/opt/zovereign/stack.py \
  --mount "type=bind,src=$(pwd)/tests,dst=/tests,readonly" \
  zovereign:test /tests/test_stack.py
```

These 21 tests use tiny local HTTP snapshots and temporary databases. They
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

## Downloader scheduling benchmark

Use `tests/benchmark_download.py` to compare an original `stack.py` with the
candidate against the same local HTTP publisher. It runs three trials with
three and six connections, uniform delays, a slow first range, repeated slow
ranges, and deterministic variable delays. Each download is 18 x 64 KiB and
must match its SHA-256. The server delays isolate scheduling; these small
synthetic timings are not mainnet throughput predictions.

After building the candidate image, save the baseline outside the repository
and mount it read-only. For example, with the baseline commit selected:

```bash
git show <baseline-commit>:docker/stack.py > /tmp/zovereign-baseline.py
docker run --rm --network none --entrypoint python3 \
  -e STACK_MODULE=/opt/zovereign/stack.py \
  --mount "type=bind,src=$(pwd)/tests,dst=/tests,readonly" \
  --mount type=bind,src=/tmp/zovereign-baseline.py,dst=/baseline.py,readonly \
  zovereign:test /tests/benchmark_download.py \
  --baseline /baseline.py --candidate /opt/zovereign/stack.py
```

Output is JSON Lines with individual durations and median/min/max/standard
deviation by scenario. Compare the same concurrency and chunk size, and report
regressions as well as improvements. No live volume or external network is used.

## Continuous integration and image publication

[The container workflow](.github/workflows/container-test.yml) validates
Compose, builds, and runs all fixture and real-binary tests separately on
native `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64) runners. Both jobs
must pass before publication. Pull requests and non-main branches only test;
they do not publish images.

On relevant pushes to `main`, or a manual run on `main`, the publish job
loads the exact tested images from short-lived workflow artifacts and pushes
them to `ghcr.io/zcashlabs/zovereign`. It assembles a manifest containing both
architectures under `sha-<full-commit>` and verifies its platform list before
updating `latest`. Per-architecture tags are `sha-<full-commit>-amd64` and
`sha-<full-commit>-arm64`. Docker selects the native architecture automatically.
Only the publish job has registry write permission. Publication uses the
workflow's `GITHUB_TOKEN`; no registry password is required in repository
secrets. Forks use their own repository namespace.

Zakura and grpcurl use pinned multi-platform upstream images. The upstream
lightwalletd v0.6.0 image is amd64-only, so both architectures compile the same
commit (`e7436d2adb9ab5828eb8a27de534e16218841fc2`) with Go 1.27.3. The Dockerfile
pins the Go image digest and verifies the source tarball's SHA-256. Go modules
are checked against upstream `go.sum` and built with `-mod=readonly`; CGO is
disabled, as in upstream's Dockerfile. Preserve the release version and commit
metadata when updating this build.

The Linux images also run through Docker Desktop on macOS and Windows/WSL 2.
They are not native macOS or Windows images. Other CPU architectures are not
supported by the pinned Zakura image. CI does not download mainnet snapshots:
ARM64 and Docker Desktop still need full-mainnet acceptance before claiming
those environments have completed a real restore.

### Versioned GitHub releases

Publishing `latest` creates a container package; it does not create an entry
on GitHub's Releases page. For a numbered release, run the **Release** workflow
on `main` with an unused stable version:

```bash
gh workflow run release.yml --ref main -f version=v0.1.0
```

The workflow validates the version, calls the complete native build/test and
publication pipeline, tags that tested multi-platform image with the version,
then creates the Git tag and published GitHub Release with installation notes.
Choose a new version for each release. A failed build or test prevents the
GitHub Release from being created. The release's Docker tag and source tag
refer to the same commit. The GitHub release archives contain source code;
the runnable image is in GHCR.

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
| `website/` | Static landing page, illustrations, and local preview instructions. |
| `scripts/` | Standalone snapshot creation, restore, and publication. |
| `deploy/digitalocean/`, `infra/digitalocean/` | Source publisher deployment. |

Keep runtime changes scoped and describe what was tested in your commit or PR.
Documentation should distinguish fixture coverage from a real mainnet restore.
