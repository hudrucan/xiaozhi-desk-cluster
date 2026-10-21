#!/usr/bin/env bash
# Cross-build on a development host. No command contacts a production node.
set -euo pipefail
: "${DESK_ARM64_LIBS:?Directory containing target libsqlite3.so and libz.so}"
: "${DESK_ZIG:?Absolute path to Zig 0.14.1}"
command -v cargo >/dev/null
[[ -f "$DESK_ARM64_LIBS/include/zlib.h" && -f "$DESK_ARM64_LIBS/include/zconf.h" ]]
[[ -f "$DESK_ARM64_LIBS/libsqlite3.so" && -f "$DESK_ARM64_LIBS/libz.so" ]]
wrap=$(mktemp -d)
trap 'rm -rf "$wrap"' EXIT
cat > "$wrap/cc" <<'CC'
#!/usr/bin/env bash
set -euo pipefail
args=()
for arg in "$@"; do
  case "$arg" in --target=*) ;; *) args+=("$arg");; esac
done
exec "$DESK_ZIG" cc -target aarch64-linux-gnu.2.36 -I"$DESK_ARM64_LIBS/include" -L"$DESK_ARM64_LIBS" "${args[@]}"
CC
cat > "$wrap/ar" <<'AR'
#!/usr/bin/env bash
exec "$DESK_ZIG" ar "$@"
AR
chmod 700 "$wrap/cc" "$wrap/ar"
export DESK_ARM64_LIBS DESK_ZIG
export CC_aarch64_unknown_linux_gnu="$wrap/cc"
export AR_aarch64_unknown_linux_gnu="$wrap/ar"
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER="$wrap/cc"
export SQLITE3_LIB_DIR="$DESK_ARM64_LIBS"
# libsqlite3-sys has checked-in bindings; no generated target headers required.
exec_build=(cargo build --release --locked --target aarch64-unknown-linux-gnu --manifest-path "$(dirname "$0")/Cargo.toml")
"${exec_build[@]}"
