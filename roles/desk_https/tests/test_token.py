"""Exercise the exact controller guard with synthetic, never live, credentials."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[3]


class TokenFormat(unittest.TestCase):
    def test_legacy_and_prefixed_tokens_pass_without_output_or_file_changes(self):
        for kind,token in [('legacy','A1_-'*10),('user','cfut_'+'A1_-'*12),('account','cfat_'+'A1_-'*12)]:
            with self.subTest(kind=kind):self.check('CLOUDFLARE_API_TOKEN='+token+'\n',True)

    def test_keys_malformed_lengths_and_environment_injection_are_rejected_silently(self):
        invalid={
            'empty':'',
            'short legacy':'A'*39,
            'long legacy':'A'*41,
            'truncated user token':'cfut_'+'A'*35,
            'short user payload':'cfut_'+'A'*47,
            'long account payload':'cfat_'+'A'*49,
            'global key':'cfk_'+'A'*48,
            'global key legacy length':'cfk_'+'A'*36,
            'whitespace':'cfut_'+'A'*47+' ',
            'invalid character':'cfat_'+'A'*47+'!',
            'quoted':'"'+'A'*40+'"',
            'assignment injection':'A'*40+'\nOTHER=value',
            'shell syntax':'A'*40+';echo unsafe',
        }
        for kind,token in invalid.items():
            with self.subTest(kind=kind):self.check('CLOUDFLARE_API_TOKEN='+token,False)
        for text in ['export CLOUDFLARE_API_TOKEN='+'A'*40,'OTHER='+'A'*40]:
            self.check(text,False)

    def check(self,text,accepted):
        tasks=yaml.safe_load((ROOT/'roles/desk_https/tasks/caddy.yml').read_text())
        guard=next(t for t in tasks if t['name'].startswith('Reject malformed token input'))
        self.assertTrue(guard['no_log']);self.assertFalse(guard['become']);self.assertFalse(guard['changed_when'])
        token_copy=next(t for t in tasks if t['name'].startswith('Install only the dedicated zone-scoped token'))
        self.assertTrue(token_copy['no_log']);self.assertFalse(token_copy['diff'])
        with tempfile.TemporaryDirectory(prefix='token-format-') as tmp:
            path=Path(tmp)/'cloudflare.env';path.write_text(text)
            result=subprocess.run([sys.executable,'-c',guard['ansible.builtin.command']['argv'][2],str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode==0,accepted)
            self.assertEqual(result.stdout,'');self.assertEqual(path.read_text(),text)
            token=text.partition('=')[2].strip()
            if token:self.assertNotIn(token,result.stderr)
            if accepted:self.assertEqual(result.stderr,'')
            else:self.assertIn('Invalid token environment format',result.stderr)


if __name__=='__main__':unittest.main()
