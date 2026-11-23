#!/usr/bin/env bash
# Build and test the pinned runtime image on the selected Docker engine.
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "Usage: bash scripts/test-weekly-image.sh [DOCKER_CONTEXT]" >&2
    exit 2
fi
cd "$(dirname "$0")/.."
engine=(docker)
if [[ $# -eq 1 ]]; then
    engine+=(--context "$1")
fi
unset DOCKER_DEFAULT_PLATFORM

image="zovereign:weekly-test-$(date -u +%Y%m%d%H%M%S)-$$"
"${engine[@]}" compose config --quiet
"${engine[@]}" build --load --no-cache -t "$image" .
platform=$("${engine[@]}" image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
expected=$("${engine[@]}" version --format '{{.Server.Os}}/{{.Server.Arch}}')
[[ "$platform" == "$expected" && ( "$platform" == linux/amd64 || "$platform" == linux/arm64 ) ]]
for suite in test_stack.py test_cli_progress.py; do
    "${engine[@]}" run --rm -i --entrypoint python3 -e STACK_MODULE=/opt/zovereign/stack.py \
        "$image" - -v < "tests/$suite"
done
printf 'PASS: weekly runtime image tests (%s, %s)\n' "$platform" "$image"
