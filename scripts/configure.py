#!/usr/bin/env python3
"""Preserve legacy installations and store paths relative to the project."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.new')
    with temporary.open('w', encoding='utf-8') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write('\n')
    temporary.replace(path)


def portable(path):
    if not path:
        return ''
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def migrate():
    old = ROOT / '.plamo-web'
    if old.is_dir() and DATA.exists():
        raise SystemExit('Both .plamo-web/ and data/ exist. Move the legacy installation aside or merge your data manually before setup.')
    if old.is_dir():
        old.rename(DATA)
    DATA.mkdir(mode=0o700, exist_ok=True)
    DATA.chmod(0o700)
    (DATA / 'jobs').mkdir(mode=0o700, exist_ok=True)
    (DATA / 'jobs').chmod(0o700)
    for folder in ('models/translation', 'models/asr', 'backends'):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    legacy_backend = ROOT / 'llama.cpp'
    backend = ROOT / 'backends/llama.cpp'
    if legacy_backend.is_dir() and not backend.exists():
        legacy_backend.rename(backend)
    config = DATA / 'config.json'
    cfg = json.loads(config.read_text()) if config.exists() else {}
    moves = {}
    for source in sorted(ROOT.glob('*.gguf')):
        asr = any(word in source.name.lower() for word in ('parakeet', 'whisper', 'canary'))
        destination = ROOT / 'models' / ('asr' if asr else 'translation') / source.name
        if destination.exists():
            raise SystemExit(f'Cannot migrate {source.name}: destination already exists: {destination}')
        source.rename(destination)
        moves[str(source)] = str(destination)
    for key, folder in (('model', 'translation'), ('asr_model', 'asr')):
        value = cfg.get(key)
        if not value:
            continue
        source = Path(value)
        source = source if source.is_absolute() else ROOT / source
        if str(source) in moves:
            cfg[key] = portable(moves[str(source)])
        elif source.is_file():
            try:
                source.relative_to(ROOT)
            except ValueError:
                cfg[key] = str(source)
                print(f'External {folder} model kept in place. Put it in models/{folder}/ if you want to move the whole app.')
            else:
                cfg[key] = portable(source)
    for key in ('model', 'asr_model'):
        if cfg.get(key):
            cfg[key] = portable(cfg[key])
    for metadata in (DATA / 'jobs').glob('*.json'):
        try:
            record = json.loads(metadata.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            print(f'Skipping unreadable job metadata: {metadata.name}')
            continue
        params = record.get('params', {})
        changed = False
        for key in ('llm_model', 'asr_model'):
            value = params.get(key)
            if value and str(value) in moves:
                params[key] = portable(moves[str(value)])
                changed = True
            elif value and Path(value).is_absolute():
                replacement = portable(value)
                if replacement != value:
                    params[key] = replacement
                    changed = True
        if changed:
            save_json(metadata, record)
    if cfg:
        save_json(config, cfg)
    # Deployed sources have been extracted to app/ and docs/ before migration.
    for name in ('app.py', 'index.html', 'script.js', 'style.css', 'README.md', 'DEVELOPMENT.md'):
        if (ROOT / 'app/app.py').is_file():
            (DATA / name).unlink(missing_ok=True)
    if (DATA / 'docs').is_dir() and (ROOT / 'docs/API.md').is_file():
        shutil.rmtree(DATA / 'docs')
    return cfg


def configure():
    cfg = migrate()
    if not (cfg.get('password_hash') and cfg.get('password_salt')):
        if not sys.stdin.isatty():
            raise SystemExit('First setup needs an interactive terminal to choose a web password. Run bash setup.sh in your terminal.')
        while True:
            password = getpass.getpass('New web password (at least 8 characters): ')
            if len(password) < 8 or password != password.strip() or password.lower() in ('password', 'password123', '12345678', 'abcdefgh'):
                print('Choose a less predictable password with at least 8 characters and no surrounding spaces.')
                continue
            if password != getpass.getpass('Repeat password: '):
                print('Passwords did not match.')
                continue
            break
        salt = secrets.token_bytes(16)
        cfg['password_salt'] = salt.hex()
        cfg['password_hash'] = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 250000).hex()
    cfg.pop('root', None)
    cfg.update({'llama_bin': 'backends/llama.cpp/build-studio/bin/llama-server',
                'asr_bin': 'backends/CrispASR/build-studio/bin/crispasr',
                'asr_root': 'backends/CrispASR'})
    for key in ('ffmpeg_bin', 'shutdown_bin'):
        cfg.pop(key, None)
    for key, value in (('web_port', 8765), ('backend_port', 8080), ('asr_port', 8178),
                       ('threads', 0), ('allow_sudo_shutdown', False),
                       ('allow_systemctl', None),
                       ('allow_loginctl', bool(shutil.which('loginctl')))):
        cfg.setdefault(key, value)
    for key, folder, suffixes in (('model', 'translation', {'.gguf'}), ('asr_model', 'asr', {'.gguf', '.bin'})):
        if not cfg.get(key):
            models = sorted(path for path in (ROOT / 'models' / folder).iterdir() if path.is_file() and path.suffix.lower() in suffixes)
            cfg[key] = portable(models[0]) if models else ''
    save_json(DATA / 'config.json', cfg)
    print('Configuration saved; existing password, settings, and jobs preserved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--migrate-only', action='store_true')
    args = parser.parse_args()
    migrate() if args.migrate_only else configure()
