"""Add only the reserved health gate to the applied private backend config."""
import json
from pathlib import Path

LOAD = '    lua-load /usr/local/libexec/desk-dns/maintenance-health.lua\n'
GATE = ('    option http-buffer-request\n'
        '    acl dns_health_question req.body,hex,lower -m sub 066865616c7468086465736b2d646e73047465737400\n'
        '    http-request deny deny_status 503 if dns_query dns_health_question { lua.desk_node_draining -m bool }\n')

def candidate(text):
    if LOAD not in text:
        if text.count('global\n') != 1: raise ValueError('Unexpected global section')
        text = text.replace('global\n', 'global\n' + LOAD, 1)
    if '    acl dns_health_question ' not in text:
        anchor = '    acl dns_query path /dns-query\n'
        if text.count(anchor) != 1: raise ValueError('Unexpected DNS query boundary')
        text = text.replace(anchor, anchor + GATE, 1)
    if text.count(LOAD) != 1 or text.count('lua.desk_node_draining -m bool') != 1:
        raise ValueError('Unexpected maintenance gate')
    return text

if __name__ == '__main__':
    print(json.dumps({'candidate':candidate(Path('/etc/desk-dns/backend-haproxy.cfg').read_text())}))
