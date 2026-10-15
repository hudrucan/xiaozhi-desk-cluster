#!/bin/sh
# The cluster owns this read-only policy. Do not replace it during bootstrap.
test -r "${NETALERTX_CONFIG}/app_conf_override.json"
