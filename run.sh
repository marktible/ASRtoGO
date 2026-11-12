#!/usr/bin/env bash
# Usage: ./run.sh [model.gguf] [extra llama-cli arguments]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/scripts/run-model.py" cli "$@"
