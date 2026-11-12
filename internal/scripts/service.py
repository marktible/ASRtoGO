#!/usr/bin/env python3
"""Start and stop only web processes owned by this project directory."""
import fcntl
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'internal/data'


def owned_pid(pidfile):
    try:
        pid = int(pidfile.read_text().strip())
        if pid <= 1:
            return None
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        expected = {os.fsencode(ROOT / path) for path in ('internal/app/app.py', 'app/app.py', '.plamo-web/app.py')}
        return pid if any(arg in expected for arg in command) else None
    except (OSError, ValueError):
        return None


def stop():
    for folder in (DATA, ROOT / 'data', ROOT / '.plamo-web'):
        pidfile = folder / 'web.pid'
        pid = owned_pid(pidfile)
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(250):
                if owned_pid(pidfile) is None:
                    break
                time.sleep(0.1)
            else:
                raise SystemExit('Web process did not finish stopping. Check its log before retrying.')
            print('Stopped web app and its managed model backends.')
        pidfile.unlink(missing_ok=True)


def start():
    config = DATA / 'config.json'
    if not config.is_file():
        raise SystemExit('Configuration missing. Run bash setup.sh first.')
    pidfile = DATA / 'web.pid'
    port = int(json.loads(config.read_text()).get('web_port', 8765))
    if owned_pid(pidfile):
        print('Web interface is already running.')
    else:
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('0.0.0.0', port))
        except OSError as error:
            raise SystemExit(f'Cannot listen on web port {port}: {error}')
        with (DATA / 'web.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'internal/app/app.py')],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        pidfile.write_text(str(process.pid) + '\n')
        for _ in range(100):
            if process.poll() is not None:
                pidfile.unlink(missing_ok=True)
                raise SystemExit('Web app failed to start. Check internal/data/web.log.')
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
            try:
                connection.request('GET', '/')
                if connection.getresponse().status == 200:
                    break
            except OSError:
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        else:
            process.terminate()
            process.wait(timeout=25)
            pidfile.unlink(missing_ok=True)
            raise SystemExit('Web app did not become ready. Check internal/data/web.log.')
    print(f'Local AI Studio: http://127.0.0.1:{port}')
    try:
        result = subprocess.run(['ip', '-o', '-4', 'addr', 'show', 'scope', 'global'],
                                capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            address = line.split()[3].split('/')[0]
            if not address.startswith('169.254.'):
                print(f'LAN address: http://{address}:{port}')
    except (OSError, subprocess.CalledProcessError, IndexError):
        pass


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in ('start', 'stop'):
        raise SystemExit('Usage: python3 internal/scripts/service.py start|stop')
    lock_dir = DATA if DATA.is_dir() else ROOT
    with (lock_dir / '.service.lock').open('w') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        start() if sys.argv[1] == 'start' else stop()
