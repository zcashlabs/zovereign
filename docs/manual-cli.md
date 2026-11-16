# Manual CLI verification

The same commands work in a POSIX shell on macOS and Linux hosts or VMs.
They use your selected Linux Docker engine (amd64 or arm64), including Docker
Desktop, Colima, native/rootless Docker or a named remote context. They create no
VM and do not download blockchain data. Start your engine using its normal tools
before beginning. Rust is needed to build the CLI; the images contain Python.

This small HTTP fixture exercises real Docker management. It contains neither
Zakura nor lightwalletd and cannot query Mainnet or connect a Zcash wallet.
Fresh Mainnet setup and its capacity gates remain separate acceptance checks.

## Build and select an engine

From the repository root, install the development CLI to a stable location:

```sh
cargo install --locked --path crates/zovereign-cli --root "$HOME/.local"
export PATH="$HOME/.local/bin:$PATH"
unset ZOVEREIGN_DOCKER FAKE_DOCKER_STATE
ZV_CONTEXT="$(docker context show)"
ZV_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/zovereign-manual/$ZV_CONTEXT"
zv() {
  zovereign --context "$ZV_CONTEXT" --config-dir "$ZV_CONFIG" "$@"
}
docker --context "$ZV_CONTEXT" info
zv --help
zv check
```

`zv` is only a shell shorthand for the actual CLI. It pins the context rather
than assuming a provider, and separates configurations for different contexts.
To select another engine, replace `$(docker context show)` in the assignment
above with its existing context name before running that block.
The helper and variables belong to this terminal; define them again when opening
another terminal. `check` can fail the full-Mainnet RAM gate on a small test VM.
That does not prevent adoption of the small fixture below.

## Create a manual deployment

Run once per selected engine, using separate names if a manual deployment already
exists there. These image/volume/container names belong only to this walkthrough:

```sh
docker --context "$ZV_CONTEXT" build -t zovereign-manual:v1 \
  --build-arg FIXTURE_REVISION=v1 tests/docker_fixture
docker --context "$ZV_CONTEXT" volume create zovereign-manual-data
docker --context "$ZV_CONTEXT" run -d --name zovereign-manual \
  --restart unless-stopped --stop-timeout 5 \
  --mount type=volume,src=zovereign-manual-data,dst=/data \
  -p 127.0.0.1:19067:9067 zovereign-manual:v1
zv adopt --container zovereign-manual
```

Answer `y` at confirmation prompts, or add `--yes` for unattended execution.
The named volume is on the Docker engine, including when the context is remote.
Its port is likewise on that engine; no host-local HTTP request is assumed.

## Daily commands and persistence

```sh
zv
zv status
zv health
zv --health
zv stats
zv --stats
zv logs --tail 50
zv connect
zv --json status
```

Run `status --watch`, `stats --watch` and `logs --follow` individually, stopping
each with Ctrl+C. Observe the fixture identity in `--json status`; it should
survive stop/start while the start counter increases.

```sh
zv stop
zv status
zv start
```

While intentionally stopped, `health` returns a nonzero exit status. After
starting, wait until `health` reports ready before continuing.

Stop the manual deployment when finished with `zv stop`. Keep its volume to
resume later. No global pruning or deployment-volume removal is part of this
walkthrough. See [the CLI guide](cli.md) for supported platforms, full-runtime
installation and Mainnet acceptance limits.
