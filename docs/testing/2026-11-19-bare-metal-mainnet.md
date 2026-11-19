# Zovereign CLI on bare-metal Ubuntu, 18–19 November 2026

This run exercised a fresh Zcash Mainnet installation on a dedicated Linux
server. The operator built the CLI and runtime from a combined working tree
containing the [foundation](https://github.com/zcashlabs/zovereign/pull/7),
[reliability](https://github.com/zcashlabs/zovereign/pull/10), and
[managed update](https://github.com/zcashlabs/zovereign/pull/11) changes.
The snapshot selector fix was applied during the run.

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
