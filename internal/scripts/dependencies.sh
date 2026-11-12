#!/usr/bin/env bash
set -euo pipefail

# Sourceable so setup can check dependencies before invoking Python or Git.
dependency_command() {
  local ID=${1:-unknown} ID_LIKE=${2:-}
  if (($# == 0)) && [[ -r /etc/os-release ]]; then source /etc/os-release; fi
  case " $ID $ID_LIKE " in
    *' void '*) INSTALL=(xbps-install -Sy base-devel git cmake ninja pkg-config python3 ffmpeg Vulkan-Headers vulkan-loader-devel vulkan-tools shaderc) ;;
    *' debian '*|*' ubuntu '*) INSTALL=(apt-get install -y build-essential git cmake ninja-build pkg-config python3 ffmpeg libvulkan-dev vulkan-tools glslc) ;;
    *' fedora '*|*' rhel '*|*' centos '*) INSTALL=(dnf install -y gcc gcc-c++ make git cmake ninja-build pkgconf-pkg-config python3 ffmpeg-free vulkan-loader-devel vulkan-headers vulkan-tools glslc) ;;
    *' arch '*|*' manjaro '*) INSTALL=(pacman -S --needed base-devel git cmake ninja pkgconf python ffmpeg vulkan-headers vulkan-icd-loader vulkan-tools shaderc) ;;
    *' opensuse '*|*' opensuse-leap '*|*' opensuse-tumbleweed '*|*' suse '*) INSTALL=(zypper install -y gcc gcc-c++ make git cmake ninja pkg-config python3 ffmpeg vulkan-devel vulkan-tools shaderc) ;;
    *' alpine '*) INSTALL=(apk add build-base git cmake ninja pkgconf python3 ffmpeg vulkan-headers vulkan-loader-dev vulkan-tools shaderc) ;;
    *) INSTALL=() ;;
  esac
}

check_dependencies() {
  local missing=() tool
  [[ $(uname -s) == Linux ]] || { echo 'This setup currently supports Linux.' >&2; return 1; }
  for tool in git cmake make pkg-config python3 ffmpeg glslc vulkaninfo; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  command -v "${CC:-cc}" >/dev/null 2>&1 || missing+=('C compiler (CC/cc)')
  command -v "${CXX:-c++}" >/dev/null 2>&1 || missing+=('C++ compiler (CXX/c++)')
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || missing+=('Python >= 3.10')
  fi
  if command -v cmake >/dev/null 2>&1; then
    local version
    version=$(cmake --version)
    version=${version#cmake version }
    version=${version%%$'\n'*}
    if [[ $version =~ ^([0-9]+)\.([0-9]+) ]] &&
       (( BASH_REMATCH[1] < 3 || (BASH_REMATCH[1] == 3 && BASH_REMATCH[2] < 24) )); then
      missing+=('CMake >= 3.24')
    fi
  fi
  if ! pkg-config --exists vulkan 2>/dev/null; then
    missing+=('Vulkan development headers/loader (pkg-config vulkan)')
  fi
  if ((${#missing[@]})); then
    printf 'Missing dependencies:\n' >&2
    printf '  %s\n' "${missing[@]}" >&2
    dependency_command
    if ((${#INSTALL[@]})); then
      printf '\nInstall for this distribution (as administrator):\n  ' >&2
      printf '%q ' "${INSTALL[@]}" >&2
      printf '\nThen rerun setup, or use: bash setup.sh --install-deps\n' >&2
      [[ ${INSTALL[0]} != apt-get ]] || echo 'On a fresh Debian/Ubuntu install, run apt-get update first.' >&2
    else
      echo 'Install the tools listed above using your distribution package manager.' >&2
    fi
    echo 'A Vulkan driver for your GPU must also be installed; setup does not choose or replace GPU drivers.' >&2
    return 1
  fi
  echo 'Dependencies OK (Linux, Python, C/C++, CMake, Git, FFmpeg, Vulkan SDK tools).'
}

install_dependencies() {
  dependency_command
  if ((${#INSTALL[@]} == 0)); then
    echo 'No automatic package mapping for this distribution. Install the reported dependencies manually.' >&2
    return 1
  fi
  local elevate=()
  if ((EUID != 0)); then
    if command -v sudo >/dev/null 2>&1; then elevate=(sudo)
    elif command -v doas >/dev/null 2>&1; then elevate=(doas)
    else echo 'Use your administrator account to install the reported dependencies.' >&2; return 1
    fi
  fi
  if [[ ${INSTALL[0]} == apt-get ]]; then "${elevate[@]}" apt-get update; fi
  "${elevate[@]}" "${INSTALL[@]}"
  check_dependencies
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  case "${1:---check}" in
    --check) check_dependencies ;;
    --install) install_dependencies ;;
    *) echo 'Usage: bash internal/scripts/dependencies.sh [--check|--install]' >&2; exit 2 ;;
  esac
fi
