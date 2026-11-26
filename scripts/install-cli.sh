#!/bin/sh
# Fetch this bootstrap from the official source repository. Release assets are
# verified against the identity of that repository's release workflow.
# The CLI embeds this script for self-update and sets ZOVEREIGN_INSTALL_DIR to the
# directory of the running binary; the shell PATH is then left unchanged.
set -eu

repo=https://github.com/zcashlabs/zovereign
verifier_version=v3.1.4

fail() { printf 'zovereign installer: %s\n' "$*" >&2; exit 1; }
download() { curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --output "$2" "$1"; }
digest() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum < "$1"
    else shasum -a 256 < "$1"
    fi
}
check_digest() {
    actual=$(digest "$1") || fail "Could not calculate SHA-256"
    [ "${actual%% *}" = "$2" ] || fail "SHA-256 mismatch for ${1##*/}"
}

add_path_line() {
    file=$1
    line=$2
    if [ ! -f "$file" ] || ! grep -Fqx "$line" "$file"; then
        printf '\n%s\n' "$line" >> "$file"
    fi
}

configure_path() {
    if [ "$(command -v zovereign || :)" = "$bin_dir/zovereign" ]; then return; fi
    line='if [ "$(command -v zovereign)" != "$HOME/.local/bin/zovereign" ]; then export PATH="$HOME/.local/bin:$PATH"; fi'
    case "${SHELL##*/}" in
        zsh)
            mkdir -p "${ZDOTDIR:-$HOME}"
            add_path_line "${ZDOTDIR:-$HOME}/.zshrc" "$line" ;;
        bash)
            add_path_line "$HOME/.bashrc" "$line"
            for profile in .bash_profile .bash_login .profile; do
                [ -f "$HOME/$profile" ] && break
            done
            add_path_line "$HOME/$profile" "$line" ;;
        sh|dash|ksh) add_path_line "$HOME/.profile" "$line" ;;
        fish)
            fish_dir="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d"
            mkdir -p "$fish_dir"
            add_path_line "$fish_dir/zovereign.fish" 'fish_add_path --move --prepend "$HOME/.local/bin"' ;;
        *) printf 'Add %s to your shell PATH to run zovereign by name.\n' "$bin_dir"; return ;;
    esac
    printf 'Open a new terminal or SSH session to use the updated PATH.\n'
}

main() {
    [ "$#" -le 1 ] || fail 'Usage: install-cli.sh [vMAJOR.MINOR.PATCH]'
    os=$(uname -s)
    arch=$(uname -m)
    # Prefer a native binary when an Apple Silicon terminal runs under Rosetta.
    if [ "$os" = Darwin ] && [ "$arch" = x86_64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || :)" = 1 ]; then arch=arm64; fi
    # SHA-256 digests of the official Sigstore verifier assets for verifier_version.
    case "$os/$arch" in
        Linux/x86_64|Linux/amd64) platform=linux-amd64; verifier_hash=424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa ;;
        Linux/aarch64|Linux/arm64) platform=linux-arm64; verifier_hash=ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2 ;;
        Darwin/x86_64) platform=darwin-amd64; verifier_hash=9d1e4abe2b6be279ed0d2ff706fde1ccd738747c834e3e6d44039c238867334f ;;
        Darwin/arm64) platform=darwin-arm64; verifier_hash=9f9b8cd557cf2e5b3df3373a00baa005a86b02391e3e06dd0ce37b3a1533d3a4 ;;
        *) fail "Unsupported platform: $os $arch (use macOS or Linux on Intel/AMD64 or ARM64)" ;;
    esac
    : "${HOME:?Set HOME to your home directory}"
    : "${SHELL:=sh}"
    bin_dir=${ZOVEREIGN_INSTALL_DIR:-$HOME/.local/bin}
    case "$bin_dir" in /*) ;; *) fail 'ZOVEREIGN_INSTALL_DIR must be an absolute path.' ;; esac
    command -v curl >/dev/null || fail 'Install curl and retry.'
    command -v sha256sum >/dev/null || command -v shasum >/dev/null || fail 'Install sha256sum or shasum and retry.'
    if [ "$#" -eq 1 ]; then
        version=$1
    else
        release=$(curl -fsSLI --proto '=https' --proto-redir '=https' -o /dev/null \
            -w '%{url_effective}' "$repo/releases/latest") || fail 'Could not resolve the latest release.'
        case "$release" in "$repo/releases/tag/"*) version=${release##*/} ;; *) fail 'No stable release found.' ;; esac
    fi
    case "$version" in *[!v0-9.]*|'') fail 'Invalid release tag.' ;; esac
    printf '%s\n' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || fail 'Expected a stable release tag such as v0.2.0.'
    umask 077
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/zovereign.XXXXXXXX")
    stage=
    trap 'rm -rf "$tmp"; if [ -n "$stage" ]; then rm -f "$stage"; fi' 0
    trap 'exit 1' HUP INT TERM

    base="$repo/releases/download/$version"
    asset="zovereign-$version-$platform"
    printf 'Fetching Zovereign %s for %s.\n' "$version" "$platform"
    download "$base/SHA256SUMS" "$tmp/SHA256SUMS" || fail "Could not download the CLI manifest for $version. Check that the release includes CLI binaries."
    download "$base/SHA256SUMS.sigstore.json" "$tmp/bundle"
    if command -v cosign >/dev/null 2>&1; then
        verifier=cosign
    else
        verifier="$tmp/cosign"
        printf 'Downloading the pinned Sigstore verifier for this installation.\n'
        download "https://github.com/sigstore/cosign/releases/download/$verifier_version/cosign-$platform" "$verifier"
        check_digest "$verifier" "$verifier_hash"
        chmod 700 "$verifier"
    fi
    "$verifier" verify-blob --bundle "$tmp/bundle" \
        --certificate-identity "$repo/.github/workflows/release.yml@refs/heads/main" \
        --certificate-oidc-issuer https://token.actions.githubusercontent.com "$tmp/SHA256SUMS" ||
        fail 'Release signature verification failed.'
    expected=$(awk -v asset="$asset" 'NF == 2 && $2 == asset { hash=$1; count++ }
        END { if (count != 1 || length(hash) != 64 || hash ~ /[^0-9a-f]/) exit 1; print hash }' "$tmp/SHA256SUMS") ||
        fail "Missing or invalid signed checksum for $asset"
    printf 'Release signature verified. Downloading the CLI binary.\n'
    download "$base/$asset" "$tmp/$asset"
    check_digest "$tmp/$asset" "$expected"

    mkdir -p "$bin_dir"
    [ ! -d "$bin_dir/zovereign" ] || fail "$bin_dir/zovereign is a directory"
    stage=$(mktemp "$bin_dir/.zovereign.XXXXXXXX")
    cp "$tmp/$asset" "$stage"
    chmod 755 "$stage"
    mv -f "$stage" "$bin_dir/zovereign"
    stage=
    printf 'Installed verified Zovereign %s at %s/zovereign\n' "$version" "$bin_dir"
    if [ -z "${ZOVEREIGN_INSTALL_DIR:-}" ]; then
        configure_path
        printf 'Run zovereign to start guided setup.\n'
    fi
}

main "$@"
