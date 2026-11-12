#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$ROOT/internal/data/config.json" ]] && \
   [[ -f "$ROOT/data/config.json" || -f "$ROOT/.plamo-web/config.json" ]]; then
  python3 "$ROOT/internal/scripts/service.py" stop
  python3 "$ROOT/internal/scripts/configure.py"
fi
exec python3 "$ROOT/internal/scripts/service.py" start
