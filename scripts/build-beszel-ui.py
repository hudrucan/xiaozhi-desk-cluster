#!/usr/bin/env python3
"""Build the pinned Beszel hub with the small native Add/Edit System UI patch."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

VERSION = '0.21.0'
SOURCE_SHA256 = '782378299b883d5a02fc0fad73b990a50d07c72c755df08edcf1b0530995129a'
PATCH = Path(__file__).resolve().parents[1] / 'roles/desk_monitoring/files/beszel-ui.patch'
TEST = PATCH.parents[1] / 'tests/test_agent_install.mjs'


def build(goos, goarch):
    identity = hashlib.sha256(SOURCE_SHA256.encode() + PATCH.read_bytes() + Path(__file__).read_bytes() + TEST.read_bytes()).hexdigest()
    cache = Path('/private/tmp/desk-beszel-ui-build') / identity
    binary = cache / f'beszel-{goos}-{goarch}'
    if binary.exists():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / 'source.tar.gz'
    if not archive.exists():
        urllib.request.urlretrieve(f'https://codeload.github.com/henrygd/beszel/tar.gz/refs/tags/v{VERSION}', archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError('Beszel source checksum mismatch')
    source = cache / f'beszel-{VERSION}'
    # Re-extract for a clean build after an interrupted attempt.
    with tarfile.open(archive) as tar:
        tar.extractall(cache, filter='data')
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)], cwd=source, check=True, stdout=sys.stderr)
    site = source / 'internal/site'
    subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=site, check=True, stdout=sys.stderr)
    subprocess.run(['npm', 'run', 'build'], cwd=site, check=True, stdout=sys.stderr)
    subprocess.run(['node', '--test', str(TEST)], env=dict(os.environ, BESZEL_UI_SOURCE=str(source)), check=True, stdout=sys.stderr)
    temporary = binary.with_suffix('.pending')
    environment = dict(os.environ, GOOS=goos, GOARCH=goarch, CGO_ENABLED='0')
    subprocess.run(['go', 'build', '-trimpath', '-ldflags', '-s -w -buildid=', '-o', str(temporary), './internal/cmd/hub'],
                   cwd=source, env=environment, check=True, stdout=sys.stderr)
    temporary.replace(binary)
    return binary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goos', default='linux')
    parser.add_argument('--goarch', default='arm64')
    args = parser.parse_args()
    print(build(args.goos, args.goarch))
