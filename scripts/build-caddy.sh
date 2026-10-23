#!/bin/sh
# Pure Go cross-build; no node is contacted and no credentials are needed.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out=${1:-/private/tmp/desk-caddy-package}
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)
cd "$repo/roles/desk_https/caddy"
GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -mod=readonly -trimpath -ldflags='-s -w -buildid=' -o "$out/caddy" ./cmd/desk-caddy
go version -m "$out/caddy" > "$out/build.txt"
python3 - "$out" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);binary=(p/'caddy').read_bytes()
assert binary[:4]==b'\x7fELF' and int.from_bytes(binary[18:20],'little')==183, 'Expected Linux ARM64 ELF'
manifest={'caddy':'2.11.7','cloudflare':'0.2.4','go':'1.26.9','sha256':hashlib.sha256(binary).hexdigest()}
(p/'manifest.json').write_text(json.dumps(manifest,sort_keys=True)+'\n')
PY
printf '%s\n' 'Pinned Linux ARM64 Caddy package ready; no node contacted.'
