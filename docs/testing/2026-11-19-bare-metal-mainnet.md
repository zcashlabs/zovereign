# Zovereign CLI on bare-metal Ubuntu, 18–19 November 2026

This run exercised a fresh Zcash Mainnet installation on a dedicated Linux
server. The operator built the CLI and runtime from a combined working tree
containing the [foundation](https://github.com/zcashlabs/zovereign/pull/7),
[reliability](https://github.com/zcashlabs/zovereign/pull/10), and
[managed update](https://github.com/zcashlabs/zovereign/pull/11) changes.
The snapshot selector fix was applied during the run. The [server test sequence](#server-test-sequence)
below includes the commands, expected failures, and evidence to collect on a repeat run.

| Environment | Value |
| --- | --- |
| Host | Ubuntu 24.04.5 LTS, x86_64, Intel Xeon E-2286G, 6 cores / 12 threads |
| Capacity at setup | About 31 GiB of RAM visible to Docker and 867 GiB free on the NVMe-backed data volume |
| Engine | Docker Engine 29.10.0 |
| Runtime | Zakura 1.8.7, archive database major 30, lightwalletd v0.6.0 |

## Bootstrap and wallet service

The machine passed the CLI resource checks. The first runtime then entered a
restart loop because the publisher's `latest` archive required database major
31. The [selector fix](https://github.com/viraj124/zovereign/commit/e5dabfb483baa343f0c58a9f507f207b5455b03c)
chose the highest compatible Mainnet archive, at height 3,578,300. After an
interruption during SHA-256 verification, the downloader reused all
275,656,458,550 bytes already on disk and restarted verification. The
lightwalletd snapshot was approximately 22.91 GiB.

After restoration, the CLI reported a healthy node. One health probe served
compact block 3,588,626 through the bundled node while Zakura reported height
3,588,627 and an estimated tip of 3,588,628.

## CLI workflows

| Exercise | What the operator checked |
| --- | --- |
| Daily controls and adoption | Health, stats, logs, wallet endpoint details and JSON output. A separate CLI profile adopted the existing container and checked its identity. |
| Updates | A locally built candidate with the same bundled versions replaced the runtime on its named data volume. The sequence covered retaining the predecessor, rolling back, reapplying and accepting the candidate. |
| Host reboot | Docker and the systemd monitor came back active. The operator tested both a running installation and an intentionally stopped one, then explicitly started the node and observed it serving blocks. |
| Service failure | The operator sent SIGTERM to lightwalletd inside the test container to exercise supervisor shutdown and Docker restart. The soak baseline was taken after this exercise. |

### Missing image during rollback

Rebuilding the candidate under its existing tag moved the tag to another image.
Docker could still run the current container and serve compact blocks, but
`docker image inspect` could no longer resolve the image ID recorded for it.
The CLI's update check returned `Current image missing`. A second update was
also refused because the previous container was still retained.

These read-only checks distinguished the ID recorded on the healthy container
from the ID now returned by the candidate tag. They use the shell helpers
defined in the [server test sequence](#server-test-sequence).

```bash
jq '{image,previous,pending}' "$cfg/installation.json"
current_image=$(zd container inspect "$node" --format '{{.Image}}')
zd image inspect "$current_image" --format '{{.Id}}'
zd image ls --no-trunc --filter 'reference=zovereign:*'
zd ps -a --filter "volume=$volume" --format '{{.Names}} {{.Image}} {{.Status}}'
zv health
```

During the incident, inspection by ID failed, `pending` was null and
`previous` identified the stopped snapshot-fix container on the same volume.
Its image was still available, and the journal recorded Zakura 1.8.7, database
major 30 and lightwalletd v0.6.0 for the predecessor.

The operator stopped the current container, disabled its restart policy, and
renamed it for diagnosis. CLI rollback then restored the retained snapshot-fix
container on the same volume. The node returned to health. For subsequent
update tests the operator kept a unique tag for each image under test.
Automatic rollback with a missing current image remains a CLI limitation.

## Mainnet acceptance and soak

**Both passed on the same candidate image.** The image ID reported at acceptance
and at the final check was
`sha256:ec93ac8b4932d8ca3be6902385b380c4b503258c9d7928be352732ad1f03f8c4`.

| Check | Recorded result |
| --- | --- |
| [Mainnet acceptance](../../tests/accept_mainnet.py) with `--restart` | The report dated 19 November at 05:12 UTC recorded `passed: true` and `restart_without_download: true`. Both databases were reused after restart. |
| Eight-hour soak | The baseline was taken at 05:49 UTC, after the deliberate service failure. The automatic restart count was 1 at the start and 1 at the final check. Docker reported `OOMKilled=false`. |
| Chain progress and wallet service | At 14:31 UTC, after the 13:49 UTC soak deadline, Zakura verified block 3,589,872. The final CLI probe reported `ready: true` and served compact block 3,589,892. |
| Snapshot provenance | The SHA-256 checks for the Zakura and lightwalletd `.bootstrap.json` files both returned `OK` against the saved baseline. |

This result covers the tested Linux x86_64 host and compatible replacement
images with Zakura database major 30. Database-major migrations and full
restores on other architectures require separate runs.

## Server test sequence

The commands below reproduce the exercises on a dedicated Ubuntu test host. Use
the combined CLI source with the snapshot fix applied. The original run started
with the unfixed image and rebuilt it after the catalog failure described above.
Run the blocks one at a time and stop at a failed check, except for the
intentional refusals called out below. Use a fresh test directory and volume
when repeating the run so the original evidence remains available.

| Transferred archive | SHA-256 |
| --- | --- |
| Combined source | `a889f978653827168cffcf9c0199e025db59b3fbdc93ec47bce6e6e97a4c6226` |
| Snapshot selector, regression tests, and deployment guide | `38d136cc26db18215afe766ca9f4bc4f0c749fda8bdfd4267a9560683cf3a20c` |

### 1. Identify the host and prepare the test shell

The operator used Bash, tmux, Python 3, jq, Docker Engine with Compose, and
Rust 1.89 with rustfmt and Clippy. Use a dedicated test volume and an account
with Docker access. Installing the binary and system monitor and rebooting this
test host require administrator access. Open the persistent shell first.

```bash
tmux new-session -A -s zovereign-e2e
```

In that shell, define the helpers. Repeat this block after each host reboot.
On the first run, select the context for the local Linux engine. Once setup has
recorded a context, these helpers pick it up from the installation record.

```bash
src="$HOME/zovereign-e2e-src"
cfg="$HOME/.config/zovereign-e2e"
reports="$HOME/zovereign-e2e-reports"
node=zovereign-e2e
volume=zovereign-e2e-data
mkdir -p "$reports"
unset ZOVEREIGN_DOCKER FAKE_DOCKER_STATE
ctx=$(docker context show)
if [ -f "$cfg/installation.json" ]; then
  ctx=$(jq -er '.context' "$cfg/installation.json")
fi
zd() { docker --context "$ctx" "$@"; }
zv() { /usr/local/bin/zovereign-e2e --context "$ctx" --config-dir "$cfg" "$@"; }
markers() { zd exec "$node" sha256sum /data/{zakura,lightwalletd}/.bootstrap.json; }
```

```bash
cat /etc/os-release
lscpu
free -h
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
zd info
zd run --rm hello-world
rustc --version
cargo --version
```

For an archived source tree, compare `sha256sum /tmp/zovereign-e2e-source.tgz`
with the sender's digest before extracting it into a fresh `$src`. Use the
patched source for a repeat run.

### 2. Build the CLI and run the automated suites

The counts below are test cases present in the transferred sources. The
verbose logs give the pass, fail, and skip counts for a particular run.

| Suite | Coverage |
| --- | --- |
| Rust workspace (14 unit tests) | Configuration and persisted state, service paths, Docker error handling, version contracts and preserving settings when recreating a container. |
| [CLI workflows](../../tests/test_cli_workflows.py) (31) | The compiled CLI drives a fake Docker executable. Tests cover checks, install/adopt safeguards, image selection, desired state, replacement/rollback, injected failures and monitor configuration. |
| [CLI progress](../../tests/test_cli_progress.py) (6) | Atomic progress updates, node checkpoints, unavailable RPC, full-disk reporting and child exit/signal handling. |
| [Real Docker integration](../../tests/test_cli_docker.py) (11) | Small labelled containers and named volumes exercise adoption, unsupported/missing runtimes, monitor access, intentional stops, replacement/rollback/acceptance and recovery from a seeded update journal. |
| [Runtime image tests at the fix commit](https://github.com/viraj124/zovereign/blob/e5dabfb483baa343f0c58a9f507f207b5455b03c/tests/test_stack.py) (16 originally, 20 after the fix) | HTTP fixtures exercise snapshot selection, ranged downloads, verification, extraction and offline reuse. Two tests launch the bundled binaries. One exercises supervisor shutdown, restart and child failure. The four added cases cover mixed catalogs, compatible fallback and offline reuse, no compatible archive, and malformed metadata. |

```bash
cd "$src"
set -o pipefail
(
  set -e
  cargo fmt --check
  cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
  cargo test --workspace --locked
  RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps --locked
  cargo build --locked
  export ZOVEREIGN_TEST_BINARY="$src/target/debug/zovereign"
  unset ZOVEREIGN_DOCKER_INTEGRATION
  python3 -m unittest discover -s tests -p 'test_cli_*.py' -v
  ZOVEREIGN_DOCKER_INTEGRATION=1 ZOVEREIGN_DOCKER_CONTEXT="$ctx" \
    python3 tests/test_cli_docker.py -v
) 2>&1 | tee "$reports/cli-tests.log"
```

The discovery run skips the opt-in Docker class. In the explicit Docker run,
check that the cases execute and teardown completes. A successful pipeline
returns zero. Build the patched runtime under a fresh tag and run its supervisor
suite inside the image so the real binary checks can execute.

```bash
run_id=$(date -u +%Y%m%dT%H%M%SZ)
base="zovereign:e2e-base-$run_id"
candidate="zovereign:e2e-candidate-$run_id"
zd compose config --quiet
zd build -t "$base" "$src"
zd run --rm --entrypoint python3 \
  -e STACK_MODULE=/opt/zovereign/stack.py \
  --mount "type=bind,src=$src/tests,dst=/tests,readonly" \
  "$base" /tests/test_stack.py -v 2>&1 | tee "$reports/runtime-tests.log"
install -m 0755 "$src/target/debug/zovereign" /usr/local/bin/zovereign-e2e
```

### 3. Check capacity, install Mainnet and resume the download

```bash
zv --json check | jq -e '.ready == true'
zd volume create "$volume"
zd run --rm --network none --entrypoint python3 \
  --mount "type=volume,src=$volume,dst=/data,readonly" "$base" \
  -c 'import shutil, sys; free = shutil.disk_usage("/data").free; print(f"{free / 2**30:.1f} GiB free"); sys.exit(free < 700 * 2**30)'
zv setup --local-image --image "$base" --container "$node" --volume "$volume"
zv status --watch
```

Setup checks usable Docker memory and CPU, then checks CPU compatibility and
capacity on the actual data volume. It recorded 931,457,540,096 free bytes on
this host. During a repeat run, let some download progress accumulate, press
Ctrl+C to leave the viewer, and exercise the interruption.

```bash
zv --json status > "$reports/before-download-interruption.json"
zv stop
zv start
zv logs --tail 100
zv status --watch
```

Inspect the resumed byte count and subsequent verification. In this run the
stop landed during SHA-256 verification at 100% of the Zakura download.
Verification restarted using the stored bytes. Allow restoration, lightwalletd
bootstrap and chain catch-up to finish. For the remaining healthy-state checks,
run each `status --watch` on its own and leave it with Ctrl+C once it says
**ready / healthy**. For the original catalog failure, the operator first ran
`zv stop`, tested a rebuilt `zovereign:e2e-snapshot-fix` image, applied it with
`zv update --local-image --image zovereign:e2e-snapshot-fix`, and used `zv start`
to resume on the existing data volume. A fresh run with the selector fix
already present can proceed directly to the wallet checks.

### 4. Check daily controls, Mainnet RPC and wallet gRPC

```bash
zv
zv status
zv health
zv --health
zv stats
zv --stats
zv logs --tail 50
zv connect
zv --json status | jq .
zv --json health | jq -e '.ready == true'
zv --json stats | jq .
zv --json connect | jq .
```

Exercise `zv stats --watch` and `zv logs --follow --tail 20` individually.
Query the services directly to compare the CLI's readiness result with what a
wallet client can read.

```bash
zd exec "$node" python3 -c 'import json, stack; print(json.dumps(stack.blockchain_info()))' \
  | jq '{chain,pruned,blocks,estimatedheight}'
zd exec "$node" grpcurl -plaintext 127.0.0.1:9067 cash.z.wallet.sdk.rpc.CompactTxStreamer/GetLightdInfo
zd exec "$node" grpcurl -plaintext 127.0.0.1:9067 cash.z.wallet.sdk.rpc.CompactTxStreamer/GetLatestBlock
zd inspect --format '{{json .HostConfig.PortBindings}}' "$node" | jq .
```

Expect `chain=main`, `pruned=false`, a block height near the estimated tip and a
Mainnet reply from lightwalletd. Check that gRPC is published on host loopback
at port 9067, P2P on 8233, and RPC port 8232 is private to the container.

### 5. Replace the image, roll back, and accept a verified update

Use a candidate built from the same sources and bundled versions. Keep both
tags for the whole exercise. On a healthy installation with `previous` and
`pending` both null, save its container identity and snapshot markers **once**.

```bash
zv --json health | jq -e '.ready == true'
jq -e '.previous == null and .pending == null' "$cfg/installation.json"
zd inspect --format '{{.Id}} {{.Image}}' "$node" > "$reports/baseline-identity.txt"
markers > "$reports/persistence-baseline.sha256"
zd build --label "io.zovereign.e2e.revision=$run_id" -t "$candidate" "$src"
zd image inspect --format '{{.Id}}' "$base" "$candidate"
zv update --check --local-image --image "$candidate"
```

The two IDs should differ and the check should report both `available` and
`compatible`. Apply once to exercise ordinary rollback.

```bash
zv update --local-image --image "$candidate"
zv status --watch
zv --json health | jq -e '.ready == true'
jq -e '.previous != null and .pending == null' "$cfg/installation.json"
zd ps -a --filter "volume=$volume" --format '{{.Names}} {{.Status}}'
zv update --rollback
zv status --watch
zv --json health | jq -e '.ready == true'
zd inspect --format '{{.Id}} {{.Image}}' "$node" | diff "$reports/baseline-identity.txt" -
zd exec -i "$node" sha256sum -c - < "$reports/persistence-baseline.sha256"
```

The predecessor should be stopped during replacement. After rollback, expect an
empty identity diff and two matching markers. Reapply the candidate, then run
the real Mainnet acceptance script **before** deliberately terminating a daemon.
The script checks for zero unexpected automatic restarts at entry.

```bash
zv update --local-image --image "$candidate"
zv status --watch
acceptance="$reports/mainnet-after-update-$(date -u +%Y%m%dT%H%M%SZ).json"
DOCKER_CONTEXT="$ctx" python3 "$src/tests/accept_mainnet.py" \
  --container "$node" --wait 1200 --restart --output "$acceptance"
jq -e '.passed == true and .restart_without_download == true' "$acceptance"
```

The script serves a compact block before and after restart, compares snapshot
provenance, checks for incomplete restore artifacts and looks for both database
reuse messages in the new logs. Once the assertion passes, finalize the update.

```bash
zv update --accept
jq -e '.previous == null and .pending == null and .desired_running == true' "$cfg/installation.json"
zd inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$node"
```

The expected data volume is `zovereign-e2e-data`. The separate
[missing-image incident](#missing-image-during-rollback) records how the
operator recovered the retained container.

### 6. Adopt the existing installation and exercise repair

```bash
adopt_cfg=$(mktemp -d)
zd inspect --format '{{.Id}}' "$node" > "$reports/before-adoption-id.txt"
/usr/local/bin/zovereign-e2e --context "$ctx" --config-dir "$adopt_cfg" adopt --container "$node"
/usr/local/bin/zovereign-e2e --context "$ctx" --config-dir "$adopt_cfg" --json health | jq -e '.ready == true'
zd inspect --format '{{.Id}}' "$node" | diff "$reports/before-adoption-id.txt" -
```

Run `zv adopt --container "$node"` and `zv setup --local-image --image "$base"`
against the original configured profile. Both should reject the duplicate
operation before prompting. Continue lifecycle tests through that original profile.

```bash
zv doctor
zv repair --action restart
zv status --watch
zv stop
zv agent --once --no-update-check
zv status
zv --json health
zv repair --action restart
```

Expect `ready=false` while stopped and an intentional-stop refusal from repair.
Resume with `zv start`, wait with `zv status --watch`, then check `zv health`.

### 7. Check systemd monitoring and both reboot cases

```bash
zv startup disable
zd inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$node"
zv startup enable
zd inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$node"
zv startup render --platform linux
zv startup install --system
systemctl is-enabled zovereign-monitor.service
systemctl is-active zovereign-monitor.service
zv agent --once --no-update-check
jq '{checked_at,status}' "$cfg/monitor.json"
```

Expect restart policies `no` and then `unless-stopped`. Check `monitor.json`
again after the next monitor interval and confirm `checked_at` advances. On an
existing E2E monitor, exercise `zv startup uninstall --system`, check that
`zv health` still succeeds, and reinstall it with `zv startup install --system`.

1. **Reboot while running.** From a healthy state run `reboot`. Reconnect,
   open tmux, and repeat the shell definitions in step 1. Run
   `systemctl is-active docker`, `systemctl is-active zovereign-monitor.service`,
   then `zv status --watch` and `zv health`. Both services should be active
   and the node should recover without manual start. Recheck the markers with
   `zd exec -i "$node" sha256sum -c - < "$reports/persistence-baseline.sha256"`.
2. **Reboot while intentionally stopped.** Run `zv stop` and then `reboot`.
   Reconnect and restore the shell helpers. Run `zv status`, `zv --json health`,
   `zv agent --once --no-update-check` and `zv status`. The node should remain
   stopped. Run `zv start`, wait with `zv status --watch`, and require
   `zv --json health | jq -e '.ready == true'` before the next exercise.

### 8. Terminate lightwalletd and verify supervisor recovery

Only exercise failure injection on the dedicated test installation. Start the
fault from a healthy state with `unless-stopped` enabled. Capture the restart
count and time before signalling the single lightwalletd process.

```bash
before=$(zd inspect --format '{{.RestartCount}}' "$node")
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
zd exec -i --user 0 "$node" python3 - <<'PYFAULT'
import os
from pathlib import Path
import signal

matches = []
for entry in Path("/proc").glob("[0-9]*/comm"):
    try:
        if entry.read_text().strip() == "lightwalletd":
            matches.append(int(entry.parent.name))
    except FileNotFoundError:
        pass
if len(matches) != 1:
    raise SystemExit(f"Expected one lightwalletd process, found {matches}")
os.kill(matches[0], signal.SIGTERM)
PYFAULT
```

Allow Docker to restart the stack, then wait for healthy status. The restart
count must increase by exactly one. Confirm the new logs show reuse of both
databases and contain no new snapshot download.

```bash
for attempt in {1..36}; do
  after=$(zd inspect --format '{{.RestartCount}}' "$node")
  if (( after > before )); then break; fi
  sleep 5
done
test "$after" -eq "$((before + 1))"
zv status --watch
zv --json health | jq -e '.ready == true'
zd logs --since "$since" "$node" > "$reports/service-failure-recovery.log" 2>&1
grep -F 'Reusing existing zakura database' "$reports/service-failure-recovery.log"
grep -F 'Reusing existing lightwalletd database' "$reports/service-failure-recovery.log"
! grep -F 'Downloading ' "$reports/service-failure-recovery.log"
```

### 9. Observe the eight-hour soak and collect the final evidence

After recovery, write the baseline once and leave the same container running
for at least eight hours. Read-only status, stats and logs are safe to inspect
during that interval. These two files are the starting point for comparison.

```bash
zv --json status > "$reports/soak-start-status.json"
zd inspect --format '{{.RestartCount}}' "$node" > "$reports/soak-start-restarts.txt"
```

After the interval, record the end state and evaluate it against the baseline.

```bash
zv --json health | jq -e '.ready == true'
zv stats
zv doctor
zv logs --tail 100
zd inspect --format '{{.RestartCount}}' "$node" | diff "$reports/soak-start-restarts.txt" -
zd inspect --format '{{json .State.OOMKilled}}' "$node" | jq -e '. == false'
zd exec -i "$node" sha256sum -c - < "$reports/persistence-baseline.sha256"
zv --json status > "$reports/final-status.json"
```

The [recorded results](#mainnet-acceptance-and-soak) identify the accepted image,
the final served block, the unchanged restart count and both marker matches.
Marker hashes cover the bootstrap descriptors. The health probe exercises the
live databases by reading a compact block near the tip.

### Optional client checks from another machine

The original runbook also included access through an SSH tunnel and remote CLI
adoption. These are optional extensions; the recorded acceptance result above
comes from the server probes. To try the tunnel, set `ZV_SSH_TARGET` to your
SSH destination and leave the forward running in its own terminal.

```bash
ssh -o ExitOnForwardFailure=yes -N -L 127.0.0.1:19067:127.0.0.1:9067 "$ZV_SSH_TARGET"
```

In a second terminal, run `grpcurl -plaintext 127.0.0.1:19067 cash.z.wallet.sdk.rpc.CompactTxStreamer/GetLightdInfo`.
For remote CLI management, select an SSH-backed Docker context on the client
and use a separate profile for read-only follow-up commands.

```bash
docker context create zovereign-metal --docker "host=ssh://$ZV_SSH_TARGET"
docker --context zovereign-metal info
remote_cfg="$HOME/.config/zovereign-e2e-remote"
./target/debug/zovereign --context zovereign-metal --config-dir "$remote_cfg" adopt --container zovereign-e2e
./target/debug/zovereign --context zovereign-metal --config-dir "$remote_cfg" health
```
