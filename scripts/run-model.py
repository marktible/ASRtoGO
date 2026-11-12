#!/usr/bin/env python3
"""Launch the local translation server or CLI from any working directory."""
import json
import os
from pathlib import Path
import sys

from hardware import detect

root = Path(__file__).resolve().parent.parent
mode = sys.argv[1]
args = sys.argv[2:]
config_path = root / 'data/config.json'
config = json.loads(config_path.read_text()) if config_path.is_file() else {}
if args and args[0].lower().endswith('.gguf'):
    model = Path(args.pop(0)).resolve()
else:
    selected = config.get('model')
    model = Path(selected) if selected else next(iter(sorted((root / 'models/translation').glob('*.gguf'))), None)
    if model is not None and not model.is_absolute():
        model = root / model
if model is None or not model.is_file():
    raise SystemExit('Add a translation GGUF to models/translation/, or pass its path as the first argument.')
binary = root / 'backends/llama.cpp/build-studio/bin' / ('llama-server' if mode == 'server' else 'llama-cli')
if not os.access(binary, os.X_OK):
    raise SystemExit('Backend missing. Run bash scripts/build-backends.sh first.')
threads = max(1, int(config.get('threads') or detect()['threads']))
command = [str(binary), '-m', str(model), '-ngl', '99', '-fa', 'on',
           '-c', str(config.get('context_tokens', 32768)), '-t', str(threads), '--jinja']
if mode == 'server':
    command += ['--host', '127.0.0.1', '--port', str(config.get('backend_port', 8080)),
                '--alias', 'local-translation', '-np', '1', '--temp', '0', '--no-prefill-assistant']
os.execv(binary, command + args)
