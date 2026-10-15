import hashlib,importlib.util,io,pathlib,tarfile,tempfile,unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('opcache',pathlib.Path(__file__).resolve().parents[1]/'files/prepare-opcache.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
def archive(version='8.3.29-r0',kind=tarfile.REGTYPE):
 output=io.BytesIO();value=b'fixture-module'
 with tarfile.open(fileobj=output,mode='w:gz') as t:
  for name,raw in [('.PKGINFO',('pkgname = php83-opcache\npkgver = '+version+'\narch = aarch64\n').encode()),(module.MODULE_PATH,value)]:
   info=tarfile.TarInfo(name);info.size=len(raw)
   if name==module.MODULE_PATH:info.type=kind
   t.addfile(info,io.BytesIO(raw))
 return output.getvalue(),value
class Checks(unittest.TestCase):
 def test_pinned_package_and_payload_checksums_are_required(self):
  raw,value=archive()
  with self.assertRaises(ValueError):module.module_bytes(raw)
  with patch.object(module,'PACKAGE_SHA256',hashlib.sha256(raw).hexdigest()):
   with self.assertRaises(ValueError):module.module_bytes(raw)
 def test_abi_mismatch_and_link_members_are_rejected(self):
  for raw,value in (archive('8.4.0-r0'),archive(kind=tarfile.SYMTYPE)):
   with patch.object(module,'PACKAGE_SHA256',hashlib.sha256(raw).hexdigest()),patch.object(module,'MODULE_SHA256',hashlib.sha256(value).hexdigest()):
    with self.assertRaises(ValueError):module.module_bytes(raw)
 def test_install_is_idempotent_and_extracts_only_module(self):
  raw,value=archive()
  with tempfile.TemporaryDirectory() as td,patch.object(module,'PACKAGE_SHA256',hashlib.sha256(raw).hexdigest()),patch.object(module,'MODULE_SHA256',hashlib.sha256(value).hexdigest()):
   root=pathlib.Path(td);package=root/'input.apk';package.write_bytes(raw);target=root/'opcache.so'
   self.assertTrue(module.install(package,target));self.assertFalse(module.install(package,target))
   self.assertEqual(target.read_bytes(),value);self.assertEqual(sorted(p.name for p in root.iterdir()),['input.apk','opcache.so'])
if __name__=='__main__':unittest.main()
