#!/usr/bin/env python3
"""Build the pinned official web client for the existing ntfy-rs topic API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

VERSION = '2.32.0'
SOURCE_SHA256 = 'afa46845907dc9eef6f20e25a8ec65309377aa7f754610fc0f23798f45cc0a97'
ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'roles/desk_ntfy/files/web.patch'
TEST = ROOT / 'roles/desk_ntfy/tests/test_web.mjs'
CONFIG = {
    'base_url': '', 'app_root': '/',
    # Local subscription credentials are supported; server account sessions/settings are not.
    'enable_login': False, 'require_login': False, 'enable_signup': False,
    'enable_payments': False, 'enable_reservations': False, 'enable_emails': False,
    'enable_calls': False, 'enable_web_push': False,
    'disallowed_topics': ['docs', 'static', 'file', 'app', 'account', 'settings', 'signup', 'login', 'v1', 'ready'],
}


def build(output):
    identity = hashlib.sha256(SOURCE_SHA256.encode() + PATCH.read_bytes() + Path(__file__).read_bytes() + TEST.read_bytes()).hexdigest()
    cache = Path('/private/tmp/desk-ntfy-web-build') / identity
    archive = cache / 'web.tar.gz'
    if not archive.exists():
        cache.mkdir(parents=True, exist_ok=True)
        source_archive = cache.parent / f'source-v{VERSION}.tar.gz'
        if not source_archive.exists():
            urllib.request.urlretrieve(f'https://codeload.github.com/binwiederhier/ntfy/tar.gz/refs/tags/v{VERSION}', source_archive)
        if hashlib.sha256(source_archive.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise RuntimeError('NTFY web source checksum mismatch')
        with tarfile.open(source_archive) as source:
            source.extractall(cache, filter='data')
        source = cache / f'ntfy-{VERSION}'
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)], cwd=source, check=True, stdout=sys.stderr)
        subprocess.run(['node', '--test', str(TEST)], env=dict(os.environ, NTFY_WEB_SOURCE=str(source)), check=True, stdout=sys.stderr)
        web = source / 'web'
        subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=web, check=True, stdout=sys.stderr)
        subprocess.run(['npm', 'run', 'build'], cwd=web, check=True, stdout=sys.stderr)
        assets = web / 'build'
        (assets / 'config.js').write_text('var config = ' + json.dumps(dict(CONFIG, config_hash=identity)) + ';\n')
        shutil.copyfile(assets / 'index.html', assets / 'app.html')
        (assets / 'manifest.webmanifest').write_text(json.dumps({'name': 'NTFY', 'short_name': 'NTFY', 'start_url': '/', 'display': 'standalone'}))
        for license_file in ['LICENSE', 'LICENSE.Apache-2.0', 'LICENSE.GPL-2.0']:
            if (source / license_file).exists():
                shutil.copyfile(source / license_file, assets / license_file)
        pending = archive.with_suffix('.pending')
        with tarfile.open(pending, 'w:gz') as tar:
            for path in sorted(assets.rglob('*')):
                if path.is_file() and path.suffix != '.map':
                    tar.add(path, arcname=str(path.relative_to(assets)))
        pending.replace(archive)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive, output / 'web.tar.gz')
    (output / 'web-sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
    print(output / 'web.tar.gz')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/private/tmp/desk-ntfy-package'))
    build(parser.parse_args().output)
