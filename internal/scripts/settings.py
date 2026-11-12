#!/usr/bin/env python3
"""Interactive settings for ASRtoGO."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

import service

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'internal/data'


def change_password():
    config = DATA / 'config.json'
    if not config.is_file():
        raise ValueError('Configuration missing. Run bash setup.sh first.')
    cfg = json.loads(config.read_text(encoding='utf-8'))
    print('Choose a web password with at least 8 characters.')
    while True:
        password = getpass.getpass('New password: ')
        if len(password) < 8 or password != password.strip():
            print('Use at least 8 characters with no surrounding spaces.')
            continue
        if password.lower() in ('password', 'password123', '12345678', 'abcdefgh'):
            print('Choose a less predictable password.')
            continue
        if password != getpass.getpass('Repeat password: '):
            print('Passwords did not match.')
            continue
        break
    salt = secrets.token_bytes(16)
    cfg['password_salt'] = salt.hex()
    cfg['password_hash'] = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 250000).hex()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA,
                                         prefix='.config-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(cfg, output, indent=2, ensure_ascii=False)
            output.write('\n')
        temporary.replace(config)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    (DATA / 'login.txt').unlink(missing_ok=True)
    print('Password updated.')
    if service.owned_pid(DATA / 'web.pid'):
        print('Restarting the web app to apply the new password...')
        service.stop()
        service.start()
    else:
        print('Start the web app with: bash start.sh')


# Add future settings as menu actions here.
ACTIONS = {'1': ('Change web password', change_password)}


def main():
    parser = argparse.ArgumentParser(prog='settings.sh', description=__doc__)
    parser.parse_args()
    if not os.isatty(0):
        parser.exit(1, 'Run bash settings.sh in an interactive terminal.\n')
    while True:
        print('\nASRtoGO settings')
        for number, (label, _) in ACTIONS.items():
            print(f'  {number}. {label}')
        print('  0. Exit')
        choice = input('Select an option: ').strip()
        if choice == '0':
            return
        action = ACTIONS.get(choice)
        if action is None:
            print('Choose one of the listed options.')
            continue
        try:
            action[1]()
        except (OSError, ValueError) as error:
            print(f'Could not update settings: {error}')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\nSettings closed.')
