"""Install only the cluster-owned integration while the target Core is stopped."""
import pathlib,shutil

def configure_ntfy(root, source=pathlib.Path('/etc/desk-homeassistant/ntfy-package.yaml')):
 # Runs only through the existing stopped-Core activation/checkpoint hook.
 if not source.is_file():return
 import yaml
 from config_journal import atomic
 root=pathlib.Path(root);path=root/'configuration.yaml';text=path.read_text()
 document=yaml.compose(text)
 def mapping(node):
  if not isinstance(node,yaml.MappingNode) or node.flow_style:raise ValueError('ntfy requires block YAML mappings')
  keys=[key.value for key,_ in node.value]
  if len(keys)!=len(set(keys)):raise ValueError('Duplicate YAML keys')
  return dict((key.value,value) for key,value in node.value)
 top=mapping(document);home=top.get('homeassistant')
 if home is None:
  text=text.rstrip()+"\nhomeassistant:\n  packages:\n    desk_ntfy: !include desk_ntfy.yaml\n"
 else:
  packages=mapping(home).get('packages')
  if packages is None:
   index=home.end_mark.index
   if index and text[index-1]!="\n":text=text[:index]+"\n"+text[index:];index+=1
   indent=" "*home.value[0][0].start_mark.column;text=text[:index]+indent+"packages:\n"+indent+"  desk_ntfy: !include desk_ntfy.yaml\n"+text[index:]
  else:
   owned=mapping(packages).get('desk_ntfy')
   if owned is None:
    index=packages.end_mark.index
    if index and text[index-1]!="\n":text=text[:index]+"\n"+text[index:];index+=1
    indent=" "*packages.value[0][0].start_mark.column;text=text[:index]+indent+"desk_ntfy: !include desk_ntfy.yaml\n"+text[index:]
   elif owned.tag!='!include' or owned.value!='desk_ntfy.yaml':raise ValueError('Existing desk_ntfy package conflicts')
 target=root/'desk_ntfy.yaml'
 if path.is_symlink() or target.is_symlink():raise ValueError('Invalid ntfy configuration target')
 atomic(target,source.read_bytes())
 if text!=path.read_text():atomic(path,text.encode())


def configure(root):
 source=pathlib.Path(__file__).parent/'desk_cluster';target=pathlib.Path(root)/'custom_components/desk_cluster'
 target.mkdir(parents=True,exist_ok=True,mode=0o700)
 if target.is_symlink():raise ValueError('Cluster component must be a real directory')
 for name in ('__init__.py','manifest.json','panel.js','reminder.py','camera-card.js','camera_recordings.py','recording_view.py','camera_storage.py','recording_model.py','recording_remote.py','observation_remote.py','command_remote.py','hls_manifest.py','thumbnail_probe.py','thumbnail_cache.py','recording_thumbnails.py','thumbnail-focus.js','recording_sessions.py','recording-loading.svg'):
  p=target/name
  if p.is_symlink():raise ValueError('Cluster component file is a symlink')
  shutil.copyfile(source/name,p);p.chmod(0o600)
 contract=pathlib.Path('/usr/local/libexec/desk-camera/recording_contract.py')
 if contract.is_file():
  destination=target/'recording_contract.py'
  if destination.is_symlink():raise ValueError('Invalid recording contract target')
  shutil.copyfile(contract,destination);destination.chmod(0o600)
 policy=pathlib.Path('/etc/desk-camera/recording-core-policy.json')
 if policy.is_file():
  destination=pathlib.Path(root)/'.storage/desk_camera_recording_authority'
  if destination.is_symlink():raise ValueError('Invalid Core recording policy target')
  shutil.copyfile(policy,destination);destination.chmod(0o600)
 for source_name,destination_name in (('/usr/local/libexec/desk-camera/observation_contract.py','custom_components/desk_cluster/observation_contract.py'),('/etc/desk-camera/observation-core-policy.json','.storage/desk_camera_observation_authority'),('/usr/local/libexec/desk-camera/command_contract.py','custom_components/desk_cluster/command_contract.py'),('/etc/desk-camera/command-core-policy.json','.storage/desk_camera_command_authority')):
  source_path=pathlib.Path(source_name)
  if source_path.is_file():
   destination=pathlib.Path(root)/destination_name
   if destination.is_symlink():raise ValueError('Invalid observation target')
   shutil.copyfile(source_path,destination);destination.chmod(0o600)
 configure_ntfy(root)
 # Retire only the duplicate media platform installed by the previous owned UI.
 (target/'media_source.py').unlink(missing_ok=True)
 camera=pathlib.Path('/usr/local/libexec/desk-homeassistant/camera_config.py')
 if camera.is_file():
  from camera_config import configure as configure_camera
  configure_camera(root)

if __name__=='__main__':
 import sys
 configure(sys.argv[1])
