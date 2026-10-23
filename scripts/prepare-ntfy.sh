#!/bin/sh
# Materialize only the pinned dependency plus the native compatibility patch.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src="$repo/roles/desk_ntfy/runtime/upstream"
destination=$src
patch="$repo/roles/desk_ntfy/runtime/upstream.patch"
pin=6f9c2e2bbad93819c0fd7bfe1c06230df5878721
fresh=
scratch=$(mktemp -d)
cleanup() {
    rm -rf "$scratch"
    if [ -n "$fresh" ]; then rm -rf "$fresh"; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
if [ ! -e "$src" ] && [ ! -L "$src" ]; then
    fresh=$(mktemp -d "$(dirname "$destination")/.upstream-prepare.XXXXXX")
    src="$fresh/upstream"
    git init -q "$src"
    git -C "$src" remote add origin https://github.com/Arcturus808/ntfy-rs.git
    git -C "$src" fetch -q --depth 1 origin "$pin"
    git -C "$src" checkout -q --detach "$pin"
fi
fail() { printf '%s\n' "ntfy preparation: $*" >&2; exit 1; }
publish() {
    if [ -n "$fresh" ]; then
        [ ! -e "$destination" ] && [ ! -L "$destination" ] || fail 'upstream appeared during preparation; left untouched'
        # The fixed basename and parent target avoid moving into a concurrent
        # checkout. Same-filesystem rename publishes only a verified checkout.
        mv -n "$src" "$(dirname "$destination")/"
        [ ! -e "$src" ] || fail 'upstream appeared during publication; left untouched'
    fi
}
[ "$(git -C "$src" rev-parse HEAD)" = "$pin" ] || fail 'unexpected upstream revision'
git -C "$src" diff --cached --quiet "$pin" -- || fail 'unexpected staged upstream changes'
[ -z "$(git -C "$src" ls-files --others --exclude-standard)" ] || fail 'unexpected untracked upstream files'

# Compute exact expected contents without modifying the checkout or its index.
tree_for_patch() {
    GIT_INDEX_FILE="$scratch/index" git -C "$src" read-tree "$pin" || return 1
    GIT_INDEX_FILE="$scratch/index" git -C "$src" apply --cached "$1" || return 1
    GIT_INDEX_FILE="$scratch/index" git -C "$src" write-tree
}
matches() { git -C "$src" diff --quiet --no-ext-diff --no-textconv "$1" --; }
wanted=$(tree_for_patch "$patch") || fail 'patch does not apply to the pinned revision'
if matches "$wanted"; then publish; exit 0; fi

previous=$pin
if ! matches "$previous"; then
    previous=
    # Only repository-recorded patches are trusted migration sources. Never reset
    # arbitrary local edits, even when they are in files touched by our patch.
    git -C "$repo" log --format=%H -- roles/desk_ntfy/runtime/upstream.patch > "$scratch/history"
    while IFS= read -r revision; do
        git -C "$repo" show "$revision:roles/desk_ntfy/runtime/upstream.patch" > "$scratch/old.patch"
        if candidate=$(tree_for_patch "$scratch/old.patch" 2>/dev/null) && matches "$candidate"; then
            previous=$candidate
            break
        fi
    done < "$scratch/history"
    [ -n "$previous" ] || fail 'unexpected local upstream changes; checkout left untouched'
fi
git -C "$src" diff --binary --no-ext-diff --no-textconv "$previous" "$wanted" > "$scratch/update.patch"
git -C "$src" apply --check "$scratch/update.patch"
git -C "$src" apply "$scratch/update.patch"
matches "$wanted" || fail 'prepared checkout does not match the expected patch'
publish
