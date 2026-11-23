#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
update=false
if [[ $# == 1 && $1 == --update ]]; then update=true
elif (( $# )); then echo 'Usage: build-backends.sh [--update]' >&2; exit 2
fi
source "$ROOT/internal/scripts/dependencies.sh"
check_dependencies
mkdir -p "$ROOT/internal/backends" "$ROOT/internal/data"
chmod 700 "$ROOT/internal/data"

BUILD_JOBS="${BUILD_JOBS:-$(python3 "$ROOT/internal/scripts/hardware.py" | python3 -c 'import json,sys; print(json.load(sys.stdin)["build_jobs"])')}"
[[ $BUILD_JOBS =~ ^[1-9][0-9]*$ ]] || { echo 'BUILD_JOBS must be a positive integer.' >&2; exit 1; }
NATIVE="${GGML_NATIVE:-ON}"
LTO="${GGML_LTO:-ON}"
[[ $NATIVE == ON || $NATIVE == OFF ]] || { echo 'GGML_NATIVE must be ON or OFF.' >&2; exit 1; }
[[ $LTO == ON || $LTO == OFF ]] || { echo 'GGML_LTO must be ON or OFF.' >&2; exit 1; }

clone_backend() {
  local url=$1 directory=$2 ref=$3
  if [[ ! -d "$directory" ]]; then
    git clone --recurse-submodules "$url" "$directory"
  elif [[ ! -d "$directory/.git" && ! -f "$directory/.git" ]]; then
    echo "Refusing to replace non-Git directory: $directory" >&2
    return 1
  elif $update; then
    if [[ -n $(git -C "$directory" status --porcelain) ]]; then
      echo "Save local changes in $directory before updating." >&2
      return 1
    fi
    git -C "$directory" pull --ff-only
  fi
  # Existing checkouts are not pulled/reset implicitly. Optional refs are explicit.
  if ! $update && [[ -n $ref ]]; then git -C "$directory" checkout "$ref"; fi
  git -C "$directory" submodule update --init --recursive
}

build_backend() {
  local directory=$1
  shift
  local target=$1
  shift
  local build="$directory/build-studio"
  if $update; then build="$build_root/$(basename "$directory")"; fi
  # Native binaries and absolute CMake caches must be rebuilt after a move.
  local machine
  machine="$(python3 - "$directory" "$NATIVE" "$LTO" <<'PY'
import hashlib, os, platform, subprocess, sys
from pathlib import Path
cpu = Path('/proc/cpuinfo').read_text() if Path('/proc/cpuinfo').exists() else platform.processor()
identity = '\n'.join(line for line in cpu.splitlines() if line.startswith(('model name', 'flags', 'Features', 'CPU implementer', 'CPU part')))
system = Path('/etc/os-release').read_text() if Path('/etc/os-release').exists() else platform.platform()
compilers = ''
for key, default in (('CC', 'cc'), ('CXX', 'c++')):
    command = os.environ.get(key, default)
    compilers += command + subprocess.check_output([command, '--version'], text=True).splitlines()[0]
print(hashlib.sha256(('\n'.join(sys.argv[1:]) + identity + platform.machine() + str(platform.libc_ver()) + system + compilers).encode()).hexdigest())
PY
)"
  if [[ -d $build && (! -f $build/.studio-machine || $(cat "$build/.studio-machine") != "$machine") ]]; then
    echo "Clearing generated build cache for this machine: $build"
    rm -rf -- "$build"
  fi
  local generator=()
  if [[ ! -f $build/CMakeCache.txt ]] && command -v ninja >/dev/null 2>&1; then generator=(-G Ninja); fi
  cmake -S "$directory" -B "$build" "${generator[@]}" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON \
    -DGGML_NATIVE="$NATIVE" -DGGML_LTO="$LTO" -DGGML_VULKAN=ON \
    -DGGML_CUDA=OFF -DGGML_HIP=OFF -DGGML_METAL=OFF "$@"
  cmake --build "$build" --config Release --parallel "$BUILD_JOBS" --target "$target"
  printf '%s\n' "$machine" > "$build/.studio-machine"
}

echo "Building Release + Vulkan, native CPU=$NATIVE, LTO=$LTO, parallel jobs=$BUILD_JOBS"
build_root="$ROOT/internal/data/backend-builds"
if $update; then
  slot=a
  if [[ $(readlink "$build_root/current" || true) == a ]]; then slot=b; fi
  build_root="$build_root/$slot"
fi
clone_backend https://github.com/ggml-org/llama.cpp.git "$ROOT/internal/backends/llama.cpp" "${LLAMA_REF:-}"
clone_backend https://github.com/CrispStrobe/CrispASR.git "$ROOT/internal/backends/CrispASR" "${CRISPASR_REF:-}"
build_backend "$ROOT/internal/backends/llama.cpp" llama-server \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=ON \
  -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_UI=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF
build_backend "$ROOT/internal/backends/CrispASR" crispasr \
  -DCRISPASR_BUILD_TESTS=OFF -DCRISPASR_BUILD_EXAMPLES=ON -DCRISPASR_BUILD_SERVER=ON \
  -DCRISPASR_PORTABLE_CPU=OFF -DCRISPASR_CURL=OFF
for entry in llama.cpp:llama-server CrispASR:crispasr; do
  project=${entry%:*}
  binary="$ROOT/internal/backends/$project/build-studio/bin/${entry#*:}"
  if $update; then binary="$build_root/$project/bin/${entry#*:}"; fi
  [[ -x $binary ]] || { echo "Build did not produce $binary" >&2; exit 1; }
  "$binary" --help > "$ROOT/internal/data/$(basename "$binary")-check.log" 2>&1 || {
    echo "Backend cannot launch: $binary. Check internal/data/$(basename "$binary")-check.log" >&2
    exit 1
  }
done
if $update; then
  builds=$(dirname "$build_root")
  ln -sfn "$slot" "$builds/current.next"
  mv -Tf "$builds/current.next" "$builds/current"
elif [[ -L $build_root/current ]]; then
  # A regular rebuild (for example after relocation) has refreshed the bundled binaries.
  rm -- "$build_root/current"
fi
echo 'Both backends built successfully.'
