"""Install only the cluster-owned integration while the target Core is stopped."""
import pathlib,shutil

def configure(root):
 source=pathlib.Path(__file__).parent/'desk_cluster';target=pathlib.Path(root)/'custom_components/desk_cluster'
 target.mkdir(parents=True,exist_ok=True,mode=0o700)
 if target.is_symlink():raise ValueError('Cluster component must be a real directory')
 for name in ('__init__.py','manifest.json','panel.js'):
  p=target/name
  if p.is_symlink():raise ValueError('Cluster component file is a symlink')
  shutil.copyfile(source/name,p);p.chmod(0o600)
 camera=pathlib.Path('/usr/local/libexec/desk-homeassistant/camera_config.py')
 if camera.is_file():
  from camera_config import configure as configure_camera
  configure_camera(root)
