# Vision source-only rollout

`vision-source.yml -e xiaozhi_vision_revision=<reviewed-pushed-server-SHA>`
stages direct Git worktrees for worker/core code and checks all existing
requirements files before activation. It reuses the existing direct venv paths
and keeps runtime bundles/environment digests and Cloud revision frozen. Worker
and core entrypoints use absolute paths to the new source; their existing data
root is exposed as a read-only namespace bind mount, without a symlink or model
copy. The control plane uses its existing Git checkout to preserve its managed
bootstrap and mutable Cloud caches. Workers converge first, then relays, then
cores, one node at a time, with readiness gates and per-node unit/source rollback.
A same-source repeat does not restart services. No paid provider probe or firmware
build/flash is performed. The firmware HTTP timeout must also be updated to 45s
for a real camera smoke check.
