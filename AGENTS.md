# Agent instructions

## Project

Zovereign provides a single Linux amd64 Docker image containing Zakura 1.8.7
(archive database major 30), lightwalletd v0.6.0, and a Python bootstrap and
supervision process. It restores published mainnet snapshots, then serves
lightwalletd against the bundled node's private RPC. Read README.md,
CONTRIBUTING.md, and docs/single-container.md first.

## Code map

- `docker/stack.py`: snapshot selection, range download, checksum verification,
  staged extraction, privilege dropping, supervision, and health probes.
- `Dockerfile`: pinned upstream binaries and runtime dependencies.
- `compose.yaml` and `.env.example`: local user startup defaults.
- `tests/test_stack.py`: fixture and real-binary regressions; no mainnet download.
- `tests/accept_mainnet.py`: opt-in acceptance for an existing real deployment.
- `website/dist/`: buildless landing page, styles, JavaScript, and illustrations.
  Keep installation commands aligned with README.md, benchmark claims dated,
  and the snapshot trust boundary clear. Check JavaScript with
  `node --check website/dist/site.js`; serve it using website/README.md.
  `.github/workflows/website.yml` deploys it to GitHub Pages on changes to `main`.
- `scripts/` and `deploy/digitalocean/`: separate snapshot publisher tooling.
  Do not change that infrastructure incidentally while editing the combined image.

## Development and checks

Run Docker builds and tests on the Docker host designated by the user, including
a remote host when requested. Keep public instructions independent of any
particular VPS address, SSH access, or credentials.

```bash
docker compose config --quiet
docker build --platform linux/amd64 -t zovereign:test .
docker run --rm --entrypoint python3 \
  -e STACK_MODULE=/opt/zovereign/stack.py \
  --mount "type=bind,src=$(pwd)/tests,dst=/tests,readonly" \
  zovereign:test /tests/test_stack.py
```

Use a separate image tag for tests against a live Docker host. The fixture
command needs no production volume. Avoid downloading mainnet snapshots unless
the task requires integration validation. Do not claim a real restore passed
when only fixtures ran. CI builds, tests, and publishes on relevant main pushes.

## Runtime invariants

- Preserve persistent data. Never delete a deployment volume, run `down -v`,
  or prune volumes as ordinary cleanup. Keep unrelated work and user edits.
- Keep mainnet archive mode and RPC on `127.0.0.1:8232` inside the container.
  gRPC binds host loopback by default; remote serving requires a tunnel or TLS.
- Lock each data volume. Do not let two live nodes open the same database.
- Pin manifest identity during resumable downloads; validate byte ranges and
  the complete SHA-256 before extraction. Preserve committed download progress.
- Reject incompatible network, database major, or lightwalletd version. Reject
  unsafe tar paths, links, and special files. Install only validated staging
  directories and refuse to overwrite unrecognized existing data.
- Bootstrap and both daemons run as UID/GID 10001. Zakura consumes `ZAKURA_*`
  as native config: filter wrapper metadata from its child environment while
  retaining supported `ZAKURA_SECTION__KEY` settings and mandatory overrides.
- lightwalletd reopens `/dev/stdout`; preserve the pipe created after privilege
  dropping and forwarded by the supervisor. Test the real binary when changing
  logging, process ownership, or environment handling.
- Shut both daemons down gracefully; if either dies, stop its peer and exit so
  Docker can restart the stack. Keep process groups and PID 1 signal handling.
- Health must verify mainnet/archive RPC near the estimated tip and a real
  compact block through lightwalletd, not only open ports.

## Documentation and publication

Keep README's clean-checkout commands, Compose defaults, supported platform,
resource requirements, and config documentation aligned. Native ARM64 is not
supported; do not imply Apple Silicon emulation has passed full acceptance.
Pin upstream versions and digests together. Document database compatibility
when upgrading. Never commit credentials, `.env`, node databases, downloaded
archives, Terraform state, or raw private deployment reports. Keep the Docker
context allowlist narrow. Public registry visibility requires verification;
do not advertise anonymous pulls before they work.
