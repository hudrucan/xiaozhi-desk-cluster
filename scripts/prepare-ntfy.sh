#!/bin/sh
# Materialize only the pinned dependency plus the native auth boundary patch.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src="$repo/roles/desk_ntfy/runtime/upstream"
patch="$repo/roles/desk_ntfy/runtime/upstream.patch"
pin=6f9c2e2bbad93819c0fd7bfe1c06230df5878721
if [ ! -e "$src" ]; then
    git init -q "$src"
    git -C "$src" remote add origin https://github.com/Arcturus808/ntfy-rs.git
    git -C "$src" fetch -q --depth 1 origin "$pin"
    git -C "$src" checkout -q --detach "$pin"
    git -C "$src" apply "$patch"
fi
[ "$(git -C "$src" rev-parse HEAD)" = "$pin" ]
[ "$(git -C "$src" status --porcelain --untracked-files=all)" = ' M src/handlers/health.rs
 M src/router.rs' ]
git -C "$src" diff --binary | cmp -s - "$patch"
