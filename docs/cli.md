# Zovereign CLI (development)

The Rust workspace adds a host CLI around the existing combined Docker image.
`zovereign-core` owns configuration, Docker operations and lifecycle decisions;
`zovereign-cli` owns terminal prompts and presentation. A desktop application can
call the same library without parsing terminal output. The existing Python
runtime remains responsible for snapshots and the two node processes.

## Build and try

Requires Rust 1.89+ to build, and the Docker CLI with access to a running Linux
Docker engine to operate the stack. Compose, Node.js, Python and Rust are not
required on an end user's host when using a prebuilt CLI binary. The runtime
image contains its own Python.

```bash
cargo build --locked
./target/debug/zovereign --help
./target/debug/zovereign check
./target/debug/zovereign setup
# Or adopt a container installed through the existing README:
./target/debug/zovereign adopt --container zovereign
```

With no command, the CLI shows status if configured, or starts guided setup.
Use `--yes` for unattended setup/adoption. Setup checks engine OS, native CPU
architecture, Docker's usable CPU/RAM, CPU instructions inside the Docker VM,
and free space on the actual named volume. It currently requires 4 CPUs,
15 GiB usable RAM (allocate 16 GiB), and 700 GiB free on a planned 1 TB SSD.
The storage gate is a conservative initial policy, not a precise prediction of
extraction size or future chain growth. Runtime extraction reserves still apply.
It does not install Docker, resize disks, open cloud firewalls or start a stopped
Docker VM. Docker reports port conflicts at container start; existing data remains.

After setup, `status --watch` follows snapshot progress and live node checkpoints.
The published image predates structured status: rebuild the image with this
checkout's `docker/stack.py` before expecting detailed progress. `logs` and
`health` also work against the older combined image.

## Commands

```bash
zovereign start
zovereign stop
zovereign status --watch
zovereign --health                 # alias for health; exit 1 when not ready
zovereign --stats                  # alias for stats
zovereign stats --watch
zovereign logs --follow
zovereign connect
zovereign doctor
zovereign repair --action restart
```

`--json` returns structured results/errors on stdout. Watch commands emit one
JSON object per line. Docker pull progress, prompts and streamed logs use stderr.
Machine-readable runtime phases include initializing, checking_database,
downloading, verifying, restoring, starting, syncing, ready, stopped and error.
Download fields distinguish committed bytes and total compressed bytes; status
includes node height/hash and volume capacity. A status observation does not
start services or change the bootstrap state.

State lives in `$XDG_CONFIG_HOME/zovereign` or `~/.config/zovereign`; override with
`--config-dir` or `ZOVEREIGN_CONFIG_DIR`. `installation.json` records the Docker
context/engine identity, volume, image and desired running state. Mutations
acquire an exclusive file lock. The CLI never removes a database volume.
Setup uses Docker's `unless-stopped` policy; the Docker engine must already
be running. Managed updates are planned for the following change. Use the
existing manual update procedure in
[the runtime guide](single-container.md#updating-a-published-image) for now.

## VM and operating system support

The code does not depend on a particular cloud provider. Run it inside a VM over
SSH, or select a named Docker context, including a remote context:

```bash
zovereign --context my-vps --config-dir ~/.config/zovereign/my-vps setup
```

The saved engine ID prevents accidental mutations if a context later points to
a different machine. Capacity checks run on that engine's data volume, not the
laptop controlling it. Wallet bindings likewise refer to the Docker host.
Use a named context rather than a bare DOCKER_HOST override for managed setups.

The host CLI currently targets macOS and Linux hosts or VMs, using a Linux Docker
engine on amd64 or arm64. Unsupported engine operating systems and architectures
are rejected. Docker Desktop/Colima need their own VM resource allocation and
startup configuration. Nested/container VMs without Docker socket access or
required virtualization support cannot operate this stack.

The CLI CI builds Linux amd64/arm64 musl binaries and macOS Intel/Apple Silicon binaries as artifacts.
These workflows are not evidence of full mainnet acceptance on each VM or OS.
The CLI has fast tests with a fake Docker boundary and opt-in integration tests
against real Docker using small service fixtures. CI runs the latter on Linux
amd64 and arm64; macOS can run them through an existing Colima or Docker Desktop
engine. Full-runtime, reboot and mainnet acceptance on the VM matrix remain
separate release gates.

## Startup and monitoring

```bash
zovereign startup enable           # Docker unless-stopped policy
zovereign startup disable          # Docker restart policy no
zovereign startup render --platform linux
zovereign startup install          # systemd user service / macOS login agent
zovereign startup uninstall
# Linux system service, when running as an administrator:
sudo /absolute/path/to/zovereign --config-dir /absolute/config startup install --system
zovereign agent --once
```

Install/copy the CLI to a permanent path before installing its monitor. Both
Linux/systemd and macOS/launchd record the installer's `PATH`, resolved Docker
configuration directory (`DOCKER_CONFIG`, or `~/.docker`) and an explicit
`ZOVEREIGN_DOCKER` executable override when present. No Homebrew, Docker Desktop,
Colima or user-specific executable path is hard-coded. Relative PATH entries and
Docker paths are resolved at installation so the service manager's working
directory does not change their meaning. Only these Docker-related environment
settings are recorded; credentials and arbitrary shell variables are not copied.

Linux user units honor an absolute `XDG_CONFIG_HOME` and otherwise use
`~/.config/systemd/user`. System units use `/etc/systemd/system`; macOS login
agents use `~/Library/LaunchAgents`. Linux installation checks the selected
systemd manager before writing a service. On Linux hosts or containers without
a systemd manager, run `zovereign agent` through that host's
supervisor instead. Removing a monitor does not require recapturing the old
installation environment.

Run `startup install` from an environment where the selected Docker context works.
Reinstall the monitor after moving the CLI or changing its Docker environment.
The service account must have access to that context, socket and any credentials;
for example, use a user service for a rootless Docker engine owned by that user.
SSH contexts also need working unattended SSH authentication under the service
account; recording a context does not create credentials or forward an interactive
SSH agent.

To refresh an existing monitor after rebuilding the CLI (use the original
`--system` scope as well when refreshing a Linux system service):

```bash
zovereign --config-dir /absolute/config startup uninstall
zovereign --config-dir /absolute/config startup install
cat /absolute/config/monitor.json
```

Check that `checked_at` advances and `status` contains the managed container
state rather than an `error`. A Docker executable lookup failure includes the
underlying OS error. The saved engine/context remain in use when the shell's
default context changes. `startup enable` configures the container's restart
policy on any supported Docker engine and does not require systemd or launchd.

The monitor records health in `monitor.json` without contacting release services.

Docker alone restarts failed containers. The monitor never restarts stopped or
unhealthy containers, so it cannot fight an update, repair or intentional stop.
Ensure Docker itself starts independently. systemd user services need login or
lingering for unattended operation; use the explicit system service for a
suitable Linux server. macOS Docker VMs/login agents generally start after login.
Non-systemd VMs can use their existing supervisor to run `zovereign agent`.

## Diagnosis and safe recovery

`doctor` reports low disk space, OOM, unhealthy services and recent lightwalletd
corruption messages. It does not prove database integrity. Restart repair asks
for confirmation and refuses an intentional stop; use `start` explicitly to
resume. Database rebuilds and destructive repairs remain manual.

## Verification

```bash
cargo fmt --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
cargo test --workspace --locked
cargo doc --workspace --no-deps --locked
cargo build --locked
python3 -m unittest discover -s tests -p 'test_cli_*.py' -v
```

The default suite uses a fake Docker boundary for deterministic setup failures,
capacity gates, adoption, engine identity and structured progress. Real-Docker
tests are skipped unless explicitly enabled; an enabled test fails when its
binary or engine is unavailable.

### Real Docker management integration

These POSIX-shell commands are identical on macOS and Linux hosts or VMs.
Start your chosen Docker engine first using its normal platform tools. Neither
the CLI nor the test suite creates or resets a VM, starts Docker, switches Docker
contexts, or requires a particular provider. The suite selects the current named
context once and pins it for the run, including cleanup.

```bash
cd /path/to/zovereign
docker context show
docker info
cargo build --locked
ZOVEREIGN_DOCKER_INTEGRATION=1 python3 tests/test_cli_docker.py -v
```

To select a different existing engine without changing your default context,
replace `my-engine` with its context name:

```bash
ZOVEREIGN_DOCKER_CONTEXT=my-engine ZOVEREIGN_DOCKER_INTEGRATION=1 \
  python3 tests/test_cli_docker.py -v
```

For local Mac prerequisites, start Docker Desktop or your existing Colima profile
as you normally do; then select its context yourself. For Linux, start/access
Docker using that distribution's setup, including rootless Docker where appropriate.
Remote engines use named Docker contexts with existing authentication. Engine-side
checks and named volumes do not depend on the controlling host's filesystem.
For interactive command-by-command testing with a persistent small deployment,
see [the manual CLI walkthrough](manual-cli.md).

The host needs Python 3.9+ for the test harness, the Docker CLI, and a compiled
CLI. The first run pulls a small, digest-pinned Python Alpine base image, then
builds one local fixture image. Registry access is needed for that first
base pull. Set `ZOVEREIGN_TEST_BINARY`
if your binary is outside `target/debug/zovereign`. Unset `ZOVEREIGN_DOCKER`
and `FAKE_DOCKER_STATE`; the suite rejects fake-engine overrides.

Coverage uses real Docker adoption (including setup's adoption path), health,
stats, logs, connection settings, stop/start, intentional stops and durable
volume identity. Monitor templates preserve Docker executable/configuration
paths when the service PATH differs from the installing terminal.
Adoption must preserve container identity, mounts and runtime settings.

Each run has unique names and a run label. Cleanup inspects that label before
removing only its own containers, volumes and images, including after failures.
It never prunes Docker or mounts deployment data. An interrupted test process
can leave objects under the printed `zovereign-it-*` prefix; inspect them before
removing them. No Mainnet data or large disk allocation is required.

The tiny HTTP fixture follows the management contract; it contains neither
Zakura nor lightwalletd. These tests verify the host CLI's Docker lifecycle,
not Zcash synchronization, gRPC readiness, snapshot restoration, host reboot,
or a fresh full-runtime installation. Production capacity checks stay enforced.
Run the existing runtime image tests when changing the supervisor, then opt-in
mainnet acceptance on a capable host with dedicated volumes before claiming a
full restore. Never use a production volume for failure injection.

## Rust guidance

The workspace lint policy is adapted from
[BeautifulRust](https://github.com/morganlinton/BeautifulRust/tree/b9718436d77ee7bcf17cdcb6aa27628feb95d97a).
It enforces selected application and library rules without enabling every
opinionated pedantic lint.

The shared API returns `zovereign_core::Error` with source errors and documented
failure modes. `LogMode` makes streaming behavior explicit. Docker/configuration
handles are accessed through immutable methods. CLI presentation and polling
cadence remain in the terminal crate. Service changes use explicit `ServiceScope`
and `ServiceAction` modes; `record_observation()` records a single read-only probe.
