"""Pinned Core 2026.10 HTTP store migration; only called with Core stopped."""
import datetime,json,pathlib
import yaml
from config_journal import atomic,encoded

def configure(root):
 root=pathlib.Path(root);path=root/'.storage/http'
 value=json.loads(path.read_bytes())
 if value.get('key')!='http' or value.get('version')!=2 or value.get('minor_version')!=2:raise ValueError('Unsupported pinned HTTP storage')
 stable=value['data']['stable']
 desired=dict(server_host=['0.0.0.0'],server_port=8123,use_x_forwarded_for=True,trusted_proxies=['10.88.0.1/32','10.10.10.11/32','10.10.10.12/32','10.10.10.13/32'],error=None,error_message=None)
 if any(stable.get(k)!=v for k,v in desired.items()):
  stable.update(desired,created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
 value['data'].update(stable=stable,pending=None,yaml_migration_done=True)
 atomic(path,encoded(value))
 # Compose without constructing values: native !include/!secret/!env_var tags
 # remain byte-for-byte intact. Only remove the deprecated top-level HTTP block.
 path=root/'configuration.yaml';text=path.read_text();document=yaml.compose(text)
 if not isinstance(document,yaml.MappingNode):raise ValueError('Invalid Core YAML')
 blocks=[(key.start_mark.index,value.end_mark.index) for key,value in document.value if key.value=='http']
 if blocks and document.flow_style:raise ValueError('Legacy HTTP must use a block mapping')
 for start,end in reversed(blocks):text=text[:start]+text[end:]
 if blocks:atomic(path,text.encode())
