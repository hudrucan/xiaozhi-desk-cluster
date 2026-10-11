#!/usr/bin/env python3
"""Generate local ignored Beszel trial login credentials without exposing them."""
import json
import os
from pathlib import Path
import secrets


def main():
    path = Path('secret/desk-monitoring.json')
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.exists():
        print('Existing monitoring bootstrap credentials retained')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump({'email': 'admin@desk.cluster', 'password': secrets.token_urlsafe(36)}, output)
        output.write('\n')
    print('Created private monitoring bootstrap credentials; no secrets printed')


if __name__ == '__main__':
    main()
