#!/usr/bin/env python3
"""Interactive settings for ASRtoGO."""
import argparse
import fcntl
import getpass
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import urllib.error
import urllib.request

import service
import maintenance

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


def update_backends():
    # Route online updates through the web worker's idle-queue check.
    with (DATA / '.service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if service.owned_pid(DATA / 'web.pid'):
            port = int(json.loads((DATA / 'config.json').read_text()).get('web_port', 8765))
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPCookieProcessor(CookieJar()))
            headers = {'Content-Type': 'application/json'}

            def post(path, body):
                request = urllib.request.Request(f'http://127.0.0.1:{port}/api/{path}',
                                                 data=json.dumps(body).encode(), headers=headers)
                try:
                    with client.open(request, timeout=60) as response:
                        return json.load(response)
                except urllib.error.HTTPError as error:
                    raise ValueError(json.load(error).get('error', str(error))) from error

            session = post('login', {'password': getpass.getpass('Web password: ')})
            headers['X-CSRF-Token'] = session['csrf']
            try:
                post('update-backends', {})
            finally:
                post('logout', {})
            process = None
        else:
            process = maintenance.launch_update()
    print(f'Updating llama.cpp and CrispASR. Build log: {maintenance.UPDATE_LOG}', flush=True)
    if process is not None:
        process.wait()
    while maintenance.update_status() == 'running':
        time.sleep(1)
    if maintenance.update_status() != 'done':
        raise ValueError(f'Backend update failed. See {maintenance.UPDATE_LOG}')
    print('Both backends updated.')


def toggle_desktop():
    state = maintenance.desktop_state()
    if state not in ('active', 'inactive', 'failed'):
        raise ValueError(f'Display manager is {state}.')
    action = 'stop' if state == 'active' else 'start'
    if action == 'stop':
        print('Stopping the desktop closes graphical apps for all users. Save your work first.')
        print('For headless jobs, run start.sh from SSH or a Linux text console (Ctrl+Alt+F3).')
        if input('Type STOP to close the desktop: ').strip() != 'STOP':
            return
    maintenance.set_desktop(action, interactive=True)
    print('Desktop stopped.' if action == 'stop' else 'Graphical login started.')


ACTIONS = {'1': ('Change web password', change_password),
           '2': ('Update backends', update_backends),
           '3': ('Stop or restore graphical desktop', toggle_desktop)}


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
