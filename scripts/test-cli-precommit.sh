#!/usr/bin/env bash
# Rehearse the CLI installer and management commands against an already configured node.
set -euo pipefail

if [[ $# != 1 || ! -f "$1/installation.json" ]]; then
    echo "Usage: bash scripts/test-cli-precommit.sh /path/to/existing/cli-profile" >&2
    exit 2
fi
profile=$(cd "$1" && pwd -P)
context=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["context"])' "$profile/installation.json")
cd "$(dirname "$0")/.."
unset ZOVEREIGN_DOCKER FAKE_DOCKER_STATE DOCKER_DEFAULT_PLATFORM

echo "1. Build and test the CLI in $PWD"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$PWD/target}"
cargo test --workspace --locked
ZOVEREIGN_RELEASE_VERSION=0.0.0 cargo build --release --locked --bin zovereign
binary="$(cd "$CARGO_TARGET_DIR/release" && pwd -P)/zovereign"
test "$("$binary" --version)" = 'zovereign 0.0.0'
cli=("$binary" --context "$context" --config-dir "$profile")

echo "2. Check the running node and wallet endpoint"
"${cli[@]}" status
"${cli[@]}" stats
"${cli[@]}" connect
"${cli[@]}" doctor
"${cli[@]}" logs --tail 20
"${cli[@]}" --json health | jq -e '.ready == true'

echo "3. Test the installer and CLI workflows, including real Docker lifecycle tests"
env -u ZOVEREIGN_CONFIG_DIR ZOVEREIGN_TEST_BINARY="$binary" \
    ZOVEREIGN_DOCKER_CONTEXT="$context" ZOVEREIGN_DOCKER_INTEGRATION=1 \
    python3 -m unittest discover -s tests -p 'test_cli_*.py' -v

"${cli[@]}" --json health | jq -e '.ready == true'
printf 'PASS: CLI installation and Docker management workflows\n'
