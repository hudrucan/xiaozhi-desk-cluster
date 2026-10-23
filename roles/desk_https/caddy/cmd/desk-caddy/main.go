package main

import (
	_ "desk.local/caddy-proxy-v2"
	_ "github.com/caddy-dns/cloudflare"
	caddycmd "github.com/caddyserver/caddy/v2/cmd"
	_ "github.com/caddyserver/caddy/v2/modules/standard"
)

func main() { caddycmd.Main() }
