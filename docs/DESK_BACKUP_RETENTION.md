# Deployment backup retention

`desk_backup_retention_days` defaults to `3` (finite and positive). Successful
producer entrypoints call the shared role after their final gates and handlers.
Each selects only its exact owned scopes in
[the catalog](../roles/desk_backup_retention/files/catalog.json). A failed host,
handled rescue, or filtered-tag invocation suppresses automatic cleanup.

Backups older than the limit expire, except the newest of each kind; equal newest
timestamps are all retained. Ansible filenames supply their creation timestamp
because copies preserve source mtime. Rollback directories use mtime. Live files,
all `/var/lib`, journal, apt/dpkg backups, unmatched names, symlinks and hardlinks
are protected. Check-mode only lists candidates; changed means actual deletion.

For separately approved maintenance, use `playbooks/node/backup-retention.yml`
with one explicit host and `--check --diff` first, then `--step`. It also covers
catalogued legacy `desk-emmc-round2-*` and `desk-ntfy-*` directories that have no
current repository producer. It is not imported by site.yml. No timer is added.

Disable future cleanup by omitting the role or selecting no scopes. Previously
pruned backups cannot be restored by reverting code; export any required older
backup before pruning. Keep the node's newly retained rollback artifact when
reverting a deployment; do not prune during rescue.
