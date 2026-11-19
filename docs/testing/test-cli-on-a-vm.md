# Test the Zovereign CLI on your own VM

Start with **A** to check how the CLI manages containers. Choose **B** if you
also want to prove your wallet endpoint serves real Zcash Mainnet blocks.

| Path | What you need | What a pass means |
| --- | --- | --- |
| **A. CLI with small test containers** | A macOS or Linux CLI host and a Linux Docker engine on amd64 or arm64. The suite downloads a small fixture base image. | The CLI can adopt a container, start and stop it, preserve data, replace its image, and roll back on your Docker engine. No chain download or 700 GiB disk is required. |
| **B. Full Mainnet acceptance** | At least 4 CPUs and 15 GiB usable RAM visible to Docker (plan for 16 GiB), and **700 GiB free on the Docker data volume** before setup (plan for a 1 TB SSD). x86_64 guests must expose BMI2 and ADX. Allow several hours and hundreds of GB of network traffic for snapshot downloads. | The node syncs near the Mainnet tip, lightwalletd serves a real compact block, and both databases survive a container restart without another snapshot download. |

## Choose where to run the commands

The **CLI host** is the machine where you type commands and save reports. The
**Docker engine** runs the node and owns its data volume. Keep the same CLI
host, account and Docker context throughout the run. Use a dedicated test VM
for the reboot and failure-recovery exercises.

| Your setup | Where to run the guide | Which Docker resources count |
| --- | --- | --- |
| Linux VM on a cloud provider or local hypervisor | Open a shell in the guest, over SSH or its console. Use system Docker or a rootless engine owned by your test account. | CPU and RAM visible to the guest's engine, plus space on its volume. |
| Mac with Docker Desktop or Colima | Open a terminal on the Mac and start the Linux VM you normally use. Select its existing Docker context. | CPU, RAM and disk allocated to that Docker VM. For Mainnet, the Mac also needs enough physical storage for the VM disk to grow. |
| Linux or Mac workstation managing a remote VM | Stay in the workstation shell and select an existing named Docker context for the remote engine (for example, an SSH context). Build the CLI on the workstation. | CPU, RAM, volume space and published ports on the remote VM. |

Docker selects the native Linux image for amd64 (x86_64) or arm64 (aarch64).
The repository has CI coverage on both engine architectures and records a full
Mainnet restore on Linux amd64. Full Mainnet acceptance on arm64 and Docker
Desktop is still pending. [Repository platform coverage](../../README.md#quick-start)

On the CLI host, use Bash (type `bash` if your login shell is different),
Git, a C linker/build tools, Python 3.9+, jq, curl, Rust 1.89+ with Cargo,
and the Docker CLI with a BuildKit-capable engine. Start the engine using its
usual tools. Allow internet access for the checkout, packages and images,
plus snapshots and peers for B.
[Docker Engine installation](https://docs.docker.com/engine/install/) · [Rust installation](https://rustup.rs/)

## Prepare a clean checkout

The public CLI and runtime fixes are on separate branches. This guide
pins the CLI with managed updates to [PR 3](https://github.com/zcashlabs/zovereign/pull/11)
and builds the Mainnet image from [PR 1's snapshot fix](https://github.com/zcashlabs/zovereign/pull/7).

For a long SSH session you can open `tmux new-session -A -s zovereign-vm`
and start Bash there. Use a fresh directory for each engine. The default is
`$HOME/zovereign-vm-test`. To test another VM from the same workstation,
set `ZV_VM_TEST_DIR` to a different absolute path and set it again after
reconnecting. Run each block separately and continue after it succeeds.

```bash
vm_dir="${ZV_VM_TEST_DIR:-$HOME/zovereign-vm-test}"
mkdir "$vm_dir" &&
  git init -q "$vm_dir/source" &&
  git -C "$vm_dir/source" remote add origin https://github.com/viraj124/zovereign.git &&
  git -C "$vm_dir/source" fetch --depth=1 origin 1ea110acd3e33f0a64fb41fca93fe46f4232a41e &&
  git -C "$vm_dir/source" switch --detach FETCH_HEAD &&
  mkdir "$vm_dir/reports" &&
  cd "$vm_dir/source"
```

Run `docker context ls` to see the available engines. The following block
uses the active context by default. To select another, set
`export ZOVEREIGN_DOCKER_CONTEXT=my-engine` first, substituting a name from
the list. With a bare `DOCKER_HOST` override, choose a named context for that
endpoint explicitly. Confirm the displayed engine is the one you intend to test.

```bash
unset ZOVEREIGN_DOCKER FAKE_DOCKER_STATE ZOVEREIGN_TEST_BINARY DOCKER_DEFAULT_PLATFORM
(
  set -eo pipefail
  ctx="${ZOVEREIGN_DOCKER_CONTEXT:-${DOCKER_CONTEXT:-}}"
  if [[ -z "$ctx" && -n "${DOCKER_HOST:-}" ]]; then
    echo 'Set ZOVEREIGN_DOCKER_CONTEXT to a named context for this engine and retry.' >&2
    exit 1
  fi
  ctx="${ctx:-$(docker context show)}"
  uname -sm | tee "$vm_dir/reports/cli-host.txt"
  docker --context "$ctx" info --format 'engine={{.Name}} os={{.OSType}} arch={{.Architecture}} cpus={{.NCPU}} memory_bytes={{.MemTotal}} server={{.ServerVersion}}' \
    | tee "$vm_dir/reports/engine.txt"
  printf '%s\n' "$ctx" > "$vm_dir/reports/context.txt"
)
```

Expect `os=linux` and one of the supported engine architectures above. On a
Mac or remote workstation the CLI host and engine can show different OS and
CPU values. After verifying the engine, build the CLI on the CLI host.

```bash
ctx=$(cat "$vm_dir/reports/context.txt")
cargo build --locked
./target/debug/zovereign --help
```

## A. Test CLI workflows with real Docker

From the `source` directory, run the fast tests, then the opt-in suite that
actually creates containers. Bash's `pipefail` keeps a failing test visible
when output is also written to a log.

```bash
set -o pipefail
unset ZOVEREIGN_DOCKER_INTEGRATION
cargo test --workspace --locked
python3 -m unittest discover -s tests -p 'test_cli_*.py' -v \
  2>&1 | tee "$vm_dir/reports/cli-tests.log"
```

```bash
ZOVEREIGN_DOCKER_CONTEXT="$ctx" ZOVEREIGN_DOCKER_INTEGRATION=1 \
  python3 tests/test_cli_docker.py -v 2>&1 | tee "$vm_dir/reports/real-docker.log"
```

**Pass:** the first Python run finishes successfully with the Docker class
skipped. The explicit Docker run prints the selected context, executes the
fixture tests and finishes with `OK`. It creates and cleans up its own labelled
containers, volumes and images. Keep `real-docker.log` if a case fails.

You can finish here on a small VM. To operate the fixture yourself, follow the
[manual CLI walkthrough](../manual-cli.md) for health, stats, logs, updates and
rollback. A Mainnet capacity warning from `zovereign check` on a small VM is
compatible with a passing fixture suite.

## B. Test a real Mainnet node

### 1. Build the runtime and check the actual data volume

From `$vm_dir/source`, fetch the runtime revision. Create a run ID **once** in
this fresh workspace. Keep it when reconnecting so you resume the same node.

```bash
git fetch --depth=1 origin e5dabfb483baa343f0c58a9f507f207b5455b03c
git worktree add --detach "$vm_dir/runtime" FETCH_HEAD
date -u +zv-vm-%Y%m%dT%H%M%SZ > "$vm_dir/reports/run-id.txt"
```

Run this helper block now and after opening a new shell. It selects the recorded
context, a separate CLI profile, and a volume specific to this run.

```bash
vm_dir="${ZV_VM_TEST_DIR:-$HOME/zovereign-vm-test}"
reports="$vm_dir/reports"
ctx=$(cat "$reports/context.txt")
node=$(cat "$reports/run-id.txt")
volume="$node-data"
image="zovereign:$node"
candidate="$image-candidate"
unset DOCKER_DEFAULT_PLATFORM
set -o pipefail
zd() { docker --context "$ctx" "$@"; }
zv() { "$vm_dir/source/target/debug/zovereign" --context "$ctx" --config-dir "$vm_dir/profile" "$@"; }
markers() { zd exec "$node" sha256sum /data/zakura/.bootstrap.json /data/lightwalletd/.bootstrap.json; }
```

The build sends the runtime source to the selected engine. The test command
streams the test file into the container so a remote VM can run it without
mounting directories from your workstation.

```bash
zv --json check | jq -e '.ready == true'
zd build --load -t "$image" "$vm_dir/runtime"
zd run --rm -i --entrypoint python3 -e STACK_MODULE=/opt/zovereign/stack.py \
  "$image" - -v < "$vm_dir/runtime/tests/test_stack.py" 2>&1 | tee "$reports/runtime-tests.log"
```

```bash
zd volume create "$volume"
zd run --rm --network none --entrypoint python3 \
  --mount "type=volume,src=$volume,dst=/data,readonly" "$image" -c '
import shutil
free = shutil.disk_usage("/data").free
print(f"{free / 2**30:.1f} GiB free on /data")
raise SystemExit(0 if free >= 700 * 2**30 else 1)
'
```

Continue when the Docker resource check is true, the runtime suite passes,
and the final command exits successfully. If the disk check fails, provision
more space on the filesystem backing the named volume. On x86, setup also
checks the CPU instructions exposed by the hypervisor.

### 2. Install, follow sync and query the wallet endpoint

The pinned PR 3 CLI uses the local image when its tag is already present. Keep
that unique tag throughout the run. The examples use host ports 19067 for gRPC
(loopback only) and 18233 for peers. Choose unused ports on the Docker host.
For a remote context, the wallet endpoint is on the VM's loopback. The gRPC
probe below runs inside the container and works from all three setups.

```bash
zv setup --image "$image" --container "$node" --volume "$volume" \
  --grpc-port 19067 --p2p-port 18233
zv status --watch
```

Wait for **ready / healthy**, then press Ctrl+C to leave the viewer. During
download, verification, restoration and catch-up, `zv logs --tail 50` shows
what the runtime is doing. Look for `Selected Zakura mainnet archive at height`
in the bootstrap logs. Setup picks the snapshot with the most blocks that your
node can read. If the newest snapshot uses a newer database format, your node
starts from the closest compatible snapshot and syncs the remaining blocks.
If SSH disconnects, reattach with `tmux attach -t zovereign-vm` if you used
tmux, or rerun the helper block in your new shell. If you intentionally
stopped the node, use `zv start` to resume.

To test download resumption, leave the viewer once it shows a nonzero byte
count during a download. Run `zv stop`, then `zv start`. Check
`zv logs --tail 100` for the resumed byte count. Only completed ranges are
saved. Return to `zv status --watch` and wait for readiness before continuing.

```bash
zv --json health | jq -e '.ready == true'
zv stats
zv connect
zd exec "$node" grpcurl -plaintext 127.0.0.1:9067 \
  cash.z.wallet.sdk.rpc.CompactTxStreamer/GetLatestBlock
```

**Pass:** health serves a compact block near the tip, the direct request returns
a block height, and `connect` reports the VM's configured gRPC binding.

### 3. Prove restart and data reuse

Run this before injecting a service failure. The acceptance script checks
for unexpected automatic restarts, tests Mainnet RPC and gRPC, and gracefully
restarts the selected test container. Allow it to return to health.

```bash
acceptance="$reports/mainnet-acceptance-$(date -u +%Y%m%dT%H%M%SZ).json"
DOCKER_CONTEXT="$ctx" python3 "$vm_dir/source/tests/accept_mainnet.py" \
  --container "$node" --restart --output "$acceptance" &&
  jq -e '{checked_at,image,passed,restart_without_download} |
    select(.passed == true and .restart_without_download == true)' "$acceptance"
```

**Pass:** the new report shows `passed: true` and `restart_without_download: true`.
If no report is produced, inspect `zv doctor`, `zv logs --tail 100`, and the
script error. A catalog with no archive for the supported database major blocks
a fresh restore until a compatible snapshot or runtime is available.

## Optional tests on the healthy Mainnet installation

Run these in order if you want to cover the full CLI lifecycle. Use the same
test node and profile as in B. Wherever a step says **wait for healthy**, run
`zv status --watch`, press Ctrl+C once it shows `ready` and `healthy`, and
confirm with `zv --json health | jq -e '.ready == true'`.

### 4. Apply a compatible update, roll back, then accept it

Record the healthy container and its snapshot markers once. Create a second
image from the same runtime source with a different label. Keep both tags
until the test ends so Docker can still resolve both image IDs.

```bash
jq -e '.previous == null and .pending == null' "$vm_dir/profile/installation.json"
zd inspect --format '{{.Id}} {{.Image}}' "$node" > "$reports/original-container.txt"
markers > "$reports/original-markers.sha256"
zd build --load --label "io.zovereign.test-candidate=$node" -t "$candidate" "$vm_dir/runtime"
zd image inspect --format '{{.Id}}' "$image" "$candidate"
zv --json update --check --local-image --image "$candidate" | jq -e '.available and .compatible'
```

Confirm the two image IDs differ and the check returns `true` before applying.

1. Run `zv update --local-image --image "$candidate"` and wait for healthy.
   Run `zd ps -a --filter "volume=$volume"`. Expect the replacement running and its predecessor stopped.
2. Run `zv update --rollback` and wait for healthy. Both checks below must pass.

```bash
zd inspect --format '{{.Id}} {{.Image}}' "$node" | diff "$reports/original-container.txt" -
zd exec -i "$node" sha256sum -c - < "$reports/original-markers.sha256"
```

An empty diff proves the original container and image returned. Both marker
files should print `OK`. Reapply the same candidate, wait for healthy, and
repeat B.3 to get a **new** passing acceptance report for the candidate. Then
run `zv update --accept` and check that the rollback record has been cleared.

```bash
jq -e '.previous == null and .pending == null' "$vm_dir/profile/installation.json"
zd inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$node"
```

The mount name should still equal `$volume`. If an update reports
`Current image missing`, compare `zd inspect --format '{{.Image}}' "$node"`
with `zd image inspect` for that ID. Keep the volume, tags and retained
container for diagnosis. Record this as a blocked update test.

### 5. Adopt the node and test intentional stops

Use a second, empty CLI profile for adoption. Return to the original `zv`
helper for repair and start/stop operations.

```bash
adopt_dir=$(mktemp -d "$vm_dir/adopt.XXXXXX")
adopted() { "$vm_dir/source/target/debug/zovereign" --context "$ctx" --config-dir "$adopt_dir" "$@"; }
zd inspect --format '{{.Id}}' "$node" > "$reports/before-adoption.txt"
adopted adopt --container "$node"
adopted --json health | jq -e '.ready == true'
zd inspect --format '{{.Id}}' "$node" | diff "$reports/before-adoption.txt" -
```

An empty diff means adoption kept the original container. Run `zv doctor`
and `zv repair --action restart` and wait for healthy. Then run `zv stop`
and `zv agent --once --no-update-check`. Expect `zv --json health` to show
`ready: false` and `zv repair --action restart` to refuse the intentional
stop. Run `zv start` and wait for healthy.

### 6. Check startup and intentional stops across a restart

Docker restarts the container. The optional monitor writes observations on
the **CLI host**. To test the guest's own monitor, run the guide in a shell
inside that guest from the beginning. Choose the monitor check for your setup.

| CLI host | How to check the monitor |
| --- | --- |
| Linux with a systemd user session, including rootless Docker | Run `zv startup install`, then `systemctl --user is-active zovereign-monitor.service`. Check it after login. Unattended user services need lingering or equivalent session configuration. |
| Linux with systemd and a root test account using system Docker | Run `zv startup install --system`, then `systemctl is-active zovereign-monitor.service`. |
| macOS with a logged-in GUI user | Run `zv startup install`, then `launchctl print "gui/$(id -u)/io.zovereign.monitor"`. Check it again after login and once the Docker VM is available. |
| Linux with another init system | Run `zv agent --once --no-update-check` to check one observation. For a persistent monitor, configure the host's own supervisor to run `"$vm_dir/source/target/debug/zovereign" --config-dir "$vm_dir/profile" agent` using the expanded paths and the test account's Docker environment. |

Keep the CLI binary at the installed path and use a service account with
access to the saved context. With a remote context, the workstation monitor
needs unattended access to the remote engine. If a monitor already belongs to
another installation, use the one-time observation check for this profile.

1. Run `zv startup disable`, then `zd inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$node"`.
   Expect `no`. Run `zv startup enable` and repeat the inspection. Expect `unless-stopped`.
2. With the node healthy, restart the **engine's VM** using its normal controls.
   For boot or login testing, configure Docker to start at that point. On a Mac,
   restarting just the Docker Desktop or Colima VM exercises the engine-restart
   case. Record which event you tested. If the CLI shell also restarted, return
   to the same account and rerun the B.1 helper block. When `zd info` works,
   wait for healthy without running `zv start`. For an installed monitor, check
   its service and confirm `jq '{checked_at,status}' "$vm_dir/profile/monitor.json"`
   shows a fresh observation for the test node.
3. Run `zv stop` and repeat the same restart. Restore the helpers if needed.
   Run `zv agent --once --no-update-check` and
   `zv --json status | jq -e '.state == "exited" and .desired_running == false'`.
   Then run `zv start` and wait for healthy.

### 7. Verify recovery from a lightwalletd exit

On the healthy test node with `unless-stopped` enabled, send SIGTERM to the
single lightwalletd process. This exercises the runtime supervisor and Docker
restart policy. It briefly interrupts the test wallet endpoint.

```bash
before=$(zd inspect --format '{{.RestartCount}}' "$node")
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
zd exec -i --user 0 "$node" python3 - <<'PY'
import os
import signal
from pathlib import Path

pids = []
for path in Path("/proc").glob("[0-9]*/comm"):
    try:
        if path.read_text().strip() == "lightwalletd":
            pids.append(int(path.parent.name))
    except FileNotFoundError:
        pass
if len(pids) != 1:
    raise SystemExit(f"Expected exactly one lightwalletd process, found {pids}")
os.kill(pids[0], signal.SIGTERM)
PY
```

Give the supervisor time to shut down both services. Once
`zd inspect --format '{{.RestartCount}}' "$node"` increases, wait for healthy. Check that there
was exactly one automatic restart and that both databases were reused.

```bash
test "$(zd inspect --format '{{.RestartCount}}' "$node")" -eq "$((before + 1))"
zd logs --since "$since" "$node" > "$reports/service-recovery.log" 2>&1
grep -F 'Reusing existing zakura database' "$reports/service-recovery.log"
grep -F 'Reusing existing lightwalletd database' "$reports/service-recovery.log"
```

### 8. Observe the same node for eight hours

Capture the baseline once, after the node is healthy. Creating a fresh
directory protects an earlier baseline if you paste the block twice.

```bash
soak="$reports/soak"
mkdir "$soak" &&
  zv --json status > "$soak/start.json" &&
  zd inspect --format '{{.Id}} {{.RestartCount}}' "$node" > "$soak/container.txt" &&
  markers > "$soak/markers.sha256" &&
  date -u +%s > "$soak/start-epoch.txt"
```

Leave the container running for at least eight hours. You can view
`zv logs --tail 50` or `zv status` during the wait. After reconnecting,
restore the helpers in B.1. The first check below succeeds after eight hours.

```bash
(
  set -eo pipefail
  soak="$reports/soak"
  start_epoch=$(cat "$soak/start-epoch.txt")
  [[ "$start_epoch" =~ ^[0-9]+$ ]]
  test "$(( $(date -u +%s) - start_epoch ))" -ge 28800
  zv --json health | jq -e '.ready == true'
  zd inspect --format '{{.Id}} {{.RestartCount}}' "$node" | diff "$soak/container.txt" -
  zd inspect --format '{{json .State.OOMKilled}}' "$node" | jq -e '. == false'
  zd exec -i "$node" sha256sum -c - < "$soak/markers.sha256"
  zv --json status > "$soak/end.json"
  jq -es 'map(.runtime.node.blocks) | all(.[]; type == "number") and (.[1] > .[0])' \
    "$soak/start.json" "$soak/end.json"
)
```

A pass means the same container served a compact block, its restart count
held steady, Docker reported no OOM kill, both bootstrap markers matched,
and the chain advanced during the interval.

## Finish and save the results

If you used the CLI to install a monitor solely for this test, remove it with
`zv startup uninstall` (or `zv startup uninstall --system` for a system unit).
For another supervisor, remove the test service through that supervisor.

To pause the node and keep the chain for another session, run `zv stop`. To
resume, run `zv start`. Keep the named volume, CLI profile, image tag and
`reports/` directory. Record the CLI host OS/CPU, Docker engine OS/CPU and
version, free volume space, and both source revisions. Report the fixture
and Mainnet outcomes separately. Include the two acceptance flags if you ran
B, and the startup method and restart event if you ran step 6. Review reports
for host names and peer details before sharing them.
