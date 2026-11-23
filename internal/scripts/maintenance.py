"""Host controls shared by the web and terminal settings."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
UPDATE_LOG = ROOT / 'internal/data/backend-update.log'
DISPLAY_MANAGER = 'display-manager.service'


def backend_binary(kind, configured):
    project, name = ('llama.cpp', 'llama-server') if kind == 'llm' else ('CrispASR', 'crispasr')
    bundled = ROOT / 'internal/backends' / project / 'build-studio/bin' / name
    current = ROOT / 'internal/data/backend-builds/current'
    if Path(configured) == bundled and current.is_dir():
        return current.resolve() / project / 'bin' / name
    return Path(configured)


def launch_update():
    with UPDATE_LOG.open('ab') as log:
        try:
            fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A backend update is already running.') from None
        log.truncate(0)
        # The build inherits the locked log as stdout, including across a web restart.
        return subprocess.Popen(['bash', str(ROOT / 'internal/scripts/build-backends.sh'), '--update'],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                env=dict(os.environ, GIT_TERMINAL_PROMPT='0'), start_new_session=True)


def update_status():
    try:
        with UPDATE_LOG.open('rb') as log:
            try:
                fcntl.flock(log, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'running'
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - 256))
            return 'done' if log.read().rstrip().endswith(b'Both backends built successfully.') else 'failed'
    except FileNotFoundError:
        return 'idle'


def desktop_state():
    if not Path('/run/systemd/system').is_dir() or not shutil.which('systemctl'):
        return 'unavailable'
    try:
        result = subprocess.run(['systemctl', 'show', DISPLAY_MANAGER, '-p', 'LoadState', '-p', 'ActiveState'],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return 'unavailable'
    properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or properties.get('LoadState') != 'loaded':
        return 'unavailable'
    return properties.get('ActiveState', 'unavailable')


def desktop_command(action):
    if action not in ('start', 'stop'):
        raise ValueError('Choose start or stop for the graphical desktop.')
    for systemctl in ('/usr/bin/systemctl', '/bin/systemctl'):
        if os.access(systemctl, os.X_OK):
            return [systemctl, '--no-ask-password', action, DISPLAY_MANAGER]
    raise ValueError('Could not find the system systemctl executable.')


def set_desktop(action, interactive=False):
    command = desktop_command(action)
    if desktop_state() == 'unavailable':
        raise ValueError('Desktop control requires a systemd display manager.')
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode and shutil.which('sudo'):
        result = subprocess.run(['sudo', *([] if interactive else ['-n']), *command],
                                stdin=None if interactive else subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode:
        hint = ('Run bash settings.sh from a text console or SSH for administrator access.' if interactive else
                'On the PC, run bash settings.sh as the studio user and choose 4 (Authorize web desktop controls).')
        raise ValueError(f'Could not {action} the desktop. {result.stderr.strip()} {hint}')
