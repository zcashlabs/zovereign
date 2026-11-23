# Zovereign CLI

## Run your own Zcash mainnet node

The CLI manages a Docker container that runs Zakura and lightwalletd. For a
fresh node, Docker needs at least **4 CPUs, 15 GiB of usable RAM, and 700 GiB
free on its data volume**. Plan for 16 GiB of RAM and a 1 TB SSD. On Intel
and AMD machines, the CPU must expose BMI2 and ADX. If the node will run on
a Linux server, SSH into it and follow these steps there. If it will run on
your Mac, allocate those resources to Docker's Linux VM.

1. **Install the CLI and start Docker.** Once the first signed CLI release is
   published, use the [one-command installer](../README.md#install-the-cli-on-macos-or-linux)
   and open a fresh terminal or SSH session. Until then, [build from source](#build-from-source)
   and use `./target/debug/zovereign` in place of `zovereign` below.
2. **Start the node.** Run `zovereign check` to check Docker and its resources,
   then `zovereign setup`. Setup checks free space on the data volume and starts
   the snapshot download. The first sync can take several hours.
3. **Wait for readiness.** Run `zovereign status --watch`. When it shows
   `ready`, press Ctrl+C to leave the watch. The node keeps running.
4. **Connect your wallet.** Run `zovereign health` to verify that lightwalletd
   serves a recent block, then `zovereign connect` for the endpoint. The default
   address is local to the Docker host. For a wallet on another device, use
   a [trusted tunnel or TLS](single-container.md#serving-remote-clients-with-tls).

## Install a release

Use the [README install command](../README.md#install-the-cli-on-macos-or-linux)
in Bash or Zsh on your Mac or Linux host. For a specific version, use that
release's pinned command from its release notes. The downloaded script
fetches assets from that same version, checks the Sigstore signature on
`SHA256SUMS` against this project's release workflow on `main`, and checks the
binary's SHA-256 before replacing `~/.local/bin/zovereign`.

You need HTTPS access, curl, and either sha256sum or shasum. The installer
uses an existing compatible Cosign or downloads a temporary verifier whose
SHA-256 is pinned in the installer. If needed, it adds `~/.local/bin` to the
startup configuration for Bash, Zsh, Fish, or a POSIX login shell. Open a new
session and run `zovereign --version` or run `"$HOME/.local/bin/zovereign" --version`
immediately. Running the installer again updates the CLI binary. Node setup
and runtime image updates are separate CLI commands.

## Build from source

Requires Rust 1.89+ to build, and the Docker CLI with access to a running Linux
Docker engine to operate the stack. Compose, Node.js, Python and Rust are not
required on an end user's host when using a prebuilt CLI binary. The optional
release monitor needs curl. The runtime image contains its own Python.

```bash
cargo build --locked
./target/debug/zovereign --version
./target/debug/zovereign --help
./target/debug/zovereign check
```

For a new installation, run `./target/debug/zovereign setup`. To manage a
compatible container you already run with Docker or Compose, use
`./target/debug/zovereign adopt --container zovereign`, substituting its name.

With no command, the CLI shows status if configured, or starts guided setup.
Use `--yes` for unattended setup/adoption. Setup checks engine OS, native CPU
architecture, Docker's usable CPU/RAM, CPU instructions inside the Docker VM,
and free space on the actual named volume. It currently requires 4 CPUs,
15 GiB usable RAM (allocate 16 GiB), and 700 GiB free on a planned 1 TB SSD.
The storage gate is a conservative initial policy, not a precise prediction of
extraction size or future chain growth. Runtime extraction reserves still apply.
It does not install Docker, resize disks, open cloud firewalls or start a stopped
Docker VM. Docker reports port conflicts at container start; existing data remains.

Fresh setup pulls the requested registry reference even when it is cached,
then records and creates the container from the resolved immutable image ID.
A failed pull stops setup without falling back to the cached image. For a
locally built or preloaded image, explicitly select local mode:

```bash
./target/debug/zovereign setup --local-image --image zovereign:local
```

Local mode never pulls and fails if the image is missing from the selected
engine. Both modes enforce the same version and capacity checks. Setup and
adoption currently support Zakura `1.8.7`, database major `30` and lightwalletd
`v0.6.0`; other or incomplete version tuples are rejected. Update this policy
alongside the bundled image after reviewing compatibility. The runtime validates
Mainnet/archive mode and snapshot compatibility before serving compact blocks.
Adoption preserves the existing container and never refreshes its image.

After setup, `status --watch` follows snapshot progress and live node checkpoints.
If your runtime image predates structured status, use `logs` and `health` to
follow startup. Build the image from the same checkout to test detailed progress.

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
zovereign update --check
zovereign update                   # also --update
zovereign update --accept          # after the new runtime is healthy
zovereign update --rollback
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
context/engine identity, volume, desired running state and update journal.
Mutations acquire an exclusive file lock. The CLI never removes a database
volume. Restart repair requires confirmation and respects an intentional stop.
`doctor` surfaces low space, OOM, pending updates, unhealthy service checks and
recent lightwalletd corruption reports; it does not prove database integrity.
Database rebuilds, data migration and destructive repair remain manual.

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
zovereign agent --once --no-update-check
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

The monitor records health in `monitor.json` and checks GitHub releases daily with
bounded HTTP timeouts. Cached release notices appear on subsequent interactive
commands. Unknown release/source revisions are not reported as proven upgrades.
Use `agent --no-update-check` for offline installations. No telemetry is sent.

Docker alone restarts failed containers. The monitor never restarts stopped or
unhealthy containers, so it cannot fight an update, repair or intentional stop.
Ensure Docker itself starts independently. systemd user services need login or
lingering for unattended operation; use the explicit system service for a
suitable Linux server. macOS Docker VMs/login agents generally start after login.
Non-systemd VMs can use their existing supervisor to run `zovereign agent`.

## Update contract and recovery

Updates pull a candidate image, then compare the bundled Zakura version, database
major and lightwalletd version. This first implementation only replaces images
with the exact same tuple. A new bundled version needs an explicit reviewed
migration path; changing environment version strings is not a migration.
Use `update --local-image --image IMAGE` (also with `--check`) for an image
already built or loaded on the selected engine. This skips the registry pull;
the same compatibility and settings checks still apply. A missing image fails
before the running container is stopped.

The old container is stopped, its automatic restart disabled, and it is renamed
and retained. The replacement attaches the same volume. The journal is saved
before every destructive container transition; a failed create can be recovered
with `repair --action rollback`. A new image is not accepted automatically:
check `health`, then `update --accept`. Acceptance removes only the old stopped
container, never its data volume. Updates of intentionally stopped installations
leave the replacement stopped. Failed replacement containers are also retained
for diagnosis after rollback. Remove them explicitly after review.

Ports, TLS mounts, environment overrides, labels and log options are preserved
for supported Docker configurations. Adoption itself changes no runtime options.
Updates refuse unsupported custom networks/resource settings/entrypoints before
stopping services. Compose deployments using a custom network can be adopted
for daily controls but currently need the documented manual image update.

## Try a pull request

Open the PR's **Checks** tab and follow **Test and package CLI** to the successful
workflow run for the revision you want to test. While signed in to GitHub,
download the matching ZIP from the run's **Artifacts** section. Choose the machine
where you will type the command. An Intel Linux server needs the Linux AMD64
artifact even if you SSH into it from an Apple Silicon Mac.

| CLI host | Artifact |
| --- | --- |
| Linux x86_64 (Intel or AMD) | `zovereign-x86_64-unknown-linux-musl` |
| Linux ARM64 | `zovereign-aarch64-unknown-linux-musl` |
| macOS Apple Silicon | `zovereign-aarch64-apple-darwin` |
| macOS Intel | `zovereign-x86_64-apple-darwin` |

Extract the ZIP into an empty directory on that host. From that directory, run

```sh
chmod +x ./zovereign
./zovereign --version
./zovereign --help
```

With Docker running, `./zovereign check` checks the engine and host resources.
For a new node, run `./zovereign setup` and then `./zovereign status --watch`.
For an existing CLI-managed node, use the account, Docker context and configuration
directory from its original setup, then run `./zovereign status`, `./zovereign health`
and `./zovereign stats`. Set `ZOVEREIGN_CONFIG_DIR` if the node uses a custom profile.

PR binaries report the workspace version. The workflow run identifies the exact
revision. The installer downloads signed binaries from numbered GitHub Releases.
To build or test the PR yourself, follow the [VM and bare-metal test guide](testing/test-cli-on-a-vm.md#prepare-a-clean-checkout).

## Verification

Use the [VM test guide](testing/test-cli-on-a-vm.md) from a Linux guest, a Mac with a Docker VM,
or a macOS/Linux workstation with a named remote Docker context. It covers small Docker fixtures
and full Mainnet acceptance on a Linux engine with enough CPU, RAM and volume space.

```bash
cargo fmt --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
cargo test --workspace --locked
cargo doc --workspace --no-deps --locked
cargo build --locked
python3 -m unittest discover -s tests -p 'test_cli_*.py' -v
```

The default suite uses a fake Docker boundary and covers argument parsing, adoption, engine identity changes, intentional
stops, compatibility rejection before shutdown, retained-container rollback,
interrupted update recovery, TLS/settings preservation and structured progress.
The real-Docker tests below are skipped unless explicitly enabled. When enabled,
a missing CLI or unavailable engine fails the suite rather than skipping it.

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
builds two compatible image revisions and one incompatible image. Registry
access is needed for the base pull. Updates use `--local-image`. Set `ZOVEREIGN_TEST_BINARY`
if your binary is outside `target/debug/zovereign`. Unset `ZOVEREIGN_DOCKER`
and `FAKE_DOCKER_STATE`; the suite rejects fake-engine overrides.

Coverage uses real Docker adoption (including setup's adoption path), health,
stats, logs, port settings, start/stop, durable volume identity, compatible
image replacement, acceptance, rollback, incompatible/missing-image rejection,
and recovery from a seeded interrupted-update journal. Environment overrides,
read-only mounts and logging options must survive replacement. The monitor
respects intentional stops. Both native monitor formats are exercised with nondefault
Docker paths/configuration and a service environment lacking the installing shell's PATH.
Recovery seeds real stopped
and renamed containers with a saved update journal.
Adoption must preserve container identity, mounts and runtime settings, including
when rejecting an unsupported database version.

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

The Rust workspace adds a host CLI around the existing combined Docker image.
`zovereign-core` owns configuration, Docker operations and lifecycle decisions;
`zovereign-cli` owns terminal prompts and presentation. A desktop application can
call the same library without parsing terminal output. The existing Python
runtime remains responsible for snapshots and the two node processes.

The workspace lint policy is adapted from
[BeautifulRust](https://github.com/morganlinton/BeautifulRust/tree/b9718436d77ee7bcf17cdcb6aa27628feb95d97a).
It enforces selected application and library rules without enabling every
opinionated pedantic lint. The Codex skills are installed outside this repository.

The shared API returns `zovereign_core::Error` and documents failure modes.
Log streaming uses `LogMode`; service changes use `ServiceScope` and
`ServiceAction`. The CLI owns polling cadence and calls `record_observation()`
for one read-only observation. Docker/configuration fields are accessed through
immutable methods. These replace the initial unpublished API's boolean modes,
public mutable handles and opaque application error type. The serialized update
stages retain their existing names, and unrecognized stages are rejected.

Unpublished API changes from this review:

| Before | After | Purpose |
| --- | --- | --- |
| `logs(follow: bool, tail)` | `logs(mode: LogMode, tail)` | Makes log-stream behavior explicit. |
| `startup::install(..., system: bool, remove: bool)` | `startup::configure(..., ServiceScope, ServiceAction)` | Prevents mixing service scope and action flags. |
| `monitor(interval, once: bool)` | `record_observation()` | Keeps scheduling and looping in the caller. |
| `anyhow::Result<T>` in shared operations | `zovereign_core::Result<T>` | Gives callers a concrete documented error type. |
| Mutable `docker`, `store` and `root` fields | `docker()`, `store()` and `root()` accessors | Keeps installation identity encapsulated. |
