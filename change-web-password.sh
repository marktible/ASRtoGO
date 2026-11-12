#!/usr/bin/env bash
# Change the Local AI Studio web password.
# Place beside web-start.sh / web-stop.sh, then run: bash change-web-password.sh
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/data"
CONFIG="$APP/config.json"

if [[ ! -f "$CONFIG" ]]; then
  echo 'Configuration missing. Run bash setup.sh first.' >&2
  exit 1
fi
if [[ ! -t 0 ]]; then
  echo 'Run this in an interactive terminal so your new password stays private.' >&2
  exit 1
fi

python3 - "$CONFIG" <<'PY'
import getpass, hashlib, json, os, secrets, sys
from pathlib import Path

path = Path(sys.argv[1]); app = path.parent
cfg = json.loads(path.read_text())
print('Change PLaMo web password')
print('Use a memorable passphrase of at least 8 characters (for example, two words and digits).')
while True:
    pw = getpass.getpass('New password: ')
    if len(pw) < 8 or not pw.strip() or pw != pw.strip():
        print('Use at least 8 characters, with no spaces at the beginning or end.')
        continue
    if pw.lower() in ('password', 'password123', '12345678', 'abcdefgh'):
        print('That password is too easy to guess. Choose another.')
        continue
    confirm = getpass.getpass('Repeat password: ')
    if pw != confirm:
        print('Passwords did not match. Try again.')
        continue
    break
salt = secrets.token_bytes(16)
cfg['password_salt'] = salt.hex()
cfg['password_hash'] = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, 250000).hex()
# Atomic replacement; credentials remain accessible only to the local owner.
tmp = path.with_name('.config.json.new')
with tmp.open('w', encoding='utf-8') as fh:
    os.fchmod(fh.fileno(), 0o600)
    json.dump(cfg, fh, indent=2)
    fh.write('\n')
os.replace(tmp, path)
(app / 'login.txt').unlink(missing_ok=True)  # Delete obsolete plaintext random password.
print('Password updated. It is not stored in plaintext.')
PY

if [[ -f "$APP/web.pid" ]] && kill -0 "$(cat "$APP/web.pid")" 2>/dev/null; then
  echo 'Restarting web interface to apply the new password...'
  old_pid="$(cat "$APP/web.pid")"
  if [[ -f "$ROOT/web-stop.sh" ]]; then
    bash "$ROOT/web-stop.sh"
  else
    echo 'Please restart the running web server manually.' >&2
    exit 0
  fi
  for ((i=0; i<80; i++)); do
    if ! kill -0 "$old_pid" 2>/dev/null; then break; fi
    sleep 0.1
  done
  if [[ -f "$ROOT/web-start.sh" ]]; then
    bash "$ROOT/web-start.sh"
  else
    echo 'Restart the web server manually to apply the new password.'
  fi
else
  echo 'Start the web interface with: bash web-start.sh'
fi
