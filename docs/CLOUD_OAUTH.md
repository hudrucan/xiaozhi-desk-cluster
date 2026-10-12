# Cloud OAuth recovery

A ready control-plane HTTP process is not proof that Cloud reconciliation works.
Check `reconciliation.state`, `reconciliation.last_success_at` and
`configuration.sync_status` in `/api/cluster`. A cached desired revision can
remain available while periodic Drive reads fail.

If Google rejects the refresh token with `invalid_grant`, restarting the service
cannot renew authorization. Authorize again with the original Desktop OAuth
client and the Google account that owns or can access the existing Cloud state.
Do not create a different client, replace the manifest, migrate configuration or
publish a new revision to recover an expired credential.

Google External apps in Testing require an allowlisted account. Their Drive
refresh tokens expire after seven days. For a persistent deployment, review the
project's Google Auth Platform **Audience** publishing status before issuing the
replacement token; In production removes the Testing-specific expiry, but Google
can still revoke or invalidate credentials. See [Google audience settings](https://support.google.com/cloud/answer/15549945?hl=en).

Use the server's existing OAuth loopback flow; complete sign-in and consent in a
browser on the controller. The callback carries an authorization code: do not
paste callback URLs or tokens into chat or logs. See the server repository's
`docs/google-drive-oauth-setup.md` for the existing login helper and SSH callback.

Before activating a replacement:

1. Match the existing client identity and validate a read of the existing
   manifest and configuration on all three nodes using staged credentials.
2. Persist the long-lived credential in encrypted `vars/vault.yml` under
   `vault_xiaozhi_cloud_credentials`, preserving all unrelated Vault entries.
   Update the ignored controller credential too so later deployment cannot
   restore the rejected token. Do not commit plaintext credentials or access tokens.
3. Replace `/etc/xiaozhi-control-plane/drive-credentials.json` atomically, retaining
   its root ownership, service group and mode `0640`. Restart only the control
   plane, one node at a time; retain a private rollback copy until verified.
4. Require `ready` reconciliation and `synced` configuration on each node, with
   matching desired revisions and a new successful reconciliation timestamp.
   Check one further periodic refresh and the shared management VIP.

Credential recovery does not apply a new conversation runtime revision. Core,
MQTT, workers, DNS and monitoring need no restart for this recovery. Saved
configuration changes still use the existing Runtime Apply workflow.
