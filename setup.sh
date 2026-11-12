#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$ROOT/$(basename "${BASH_SOURCE[0]}")"
INSTALL_DEPS=false
START=true
for arg in "$@"; do
  case "$arg" in
    --install-deps) INSTALL_DEPS=true ;;
    --no-start) START=false ;;
    --help|-h)
      echo 'Usage: bash setup.sh [--install-deps] [--no-start]'
      echo 'Checks dependencies, builds llama.cpp + CrispASR with Vulkan, configures the app, then removes itself.'
      exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
trap 'echo "Setup failed. The setup script is retained; fix the error above and rerun it." >&2' ERR
source "$ROOT/internal/scripts/dependencies.sh"
if ! check_dependencies; then
  if "$INSTALL_DEPS"; then install_dependencies; else exit 1; fi
fi
echo 'Checking that a Vulkan device is available...'
python3 "$ROOT/internal/scripts/check-vulkan.py"
mkdir -p "$ROOT/models/translation" "$ROOT/models/asr"
bash "$ROOT/stop.sh"
python3 "$ROOT/internal/scripts/configure.py" --migrate-only
bash "$ROOT/internal/scripts/build-backends.sh"
python3 "$ROOT/internal/scripts/configure.py"
python3 -m py_compile "$ROOT/internal/app/app.py"
if "$START"; then bash "$ROOT/start.sh"; fi
echo 'Setup complete. Add your own models to models/translation/ and models/asr/.'
echo 'Use start.sh and stop.sh. To rebuild on another machine: bash internal/scripts/build-backends.sh'
rm -- "$SELF"
