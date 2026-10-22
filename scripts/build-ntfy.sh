#!/bin/sh
# Requires a Rust toolchain and a Linux ARM64 C linker (bundled SQLite/AWS-LC).
# Native ARM64 Linux: scripts/build-ntfy.sh /path/to/controller-package
# Cross build: set CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER and CC_aarch64_unknown_linux_gnu.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
"$repo/scripts/prepare-ntfy.sh"
out=${1:-/private/tmp/desk-ntfy-package}
manifest="$repo/roles/desk_ntfy/runtime/Cargo.toml"
target=${CARGO_TARGET_DIR:-/tmp/desk-ntfy-build}
export CARGO_TARGET_DIR="$target"
if [ "${DESK_NTFY_ZIG:-0}" = 1 ]; then
    cargo zigbuild --locked --release --target aarch64-unknown-linux-gnu.2.31 --manifest-path "$manifest"
else
    cargo build --locked --release --target aarch64-unknown-linux-gnu --manifest-path "$manifest"
fi
mkdir -p "$out"
cp "$target/aarch64-unknown-linux-gnu/release/desk-ntfy" "$out/desk-ntfy"
# Ansible verifies both this digest and the expected ELF architecture before installation.
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$out/desk-ntfy" | cut -d ' ' -f 1 > "$out/sha256"
else
    shasum -a 256 "$out/desk-ntfy" | cut -d ' ' -f 1 > "$out/sha256"
fi
python3 "$repo/scripts/build-ntfy-web.py" --output "$out"
printf '%s\n' 'Native ARM64 package prepared; no node was contacted.'
