#!/usr/bin/env bash
# Usage: ./translate.sh [model.gguf] [extra llama-server arguments]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/scripts/run-model.py" server "$@"
