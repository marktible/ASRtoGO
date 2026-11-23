# ASRtoGO

Translate text, transcribe audio/video, and translate subtitles in a local web app.
Runs on your Linux PC using llama.cpp and CrispASR with Vulkan.

## 1. Set up

Run from this folder as your regular user:

```bash
bash setup.sh --install-deps
```

Setup checks dependencies, builds both backends for your machine, asks for a web
password, and opens the web interface. It removes itself after success. If setup
fails, fix the reported error and run it again.

Dependency installation supports Void, Debian/Ubuntu, Fedora, Arch, openSUSE, and
Alpine. A working Vulkan GPU driver is required. Minimal Alpine installations
need Bash first. Omit `--install-deps` to check dependencies without installing.

## 2. Add models

Download your models yourself; setup does not download any.

| Task | Put models here |
| --- | --- |
| Translation | `models/translation/` (`.gguf`) |
| Speech recognition | `models/asr/` (`.gguf` or Whisper `.bin`) |

Choose compatible models and keep their required companion files together.
Refresh Settings in the web app after adding files.

## 3. Start or stop

```bash
bash start.sh
bash stop.sh
```

The start script prints the address to open on your PC or phone. Use it on a
trusted LAN/private connection; do not expose it to the public internet.

## Settings

```bash
bash settings.sh
```

In the web app, check **Settings → Models → Auto unload** to keep one model in
memory at a time during ASR + Translate. The browser remembers your choice for
future combined jobs.

In the web app, **Settings → Update backends** updates and rebuilds llama.cpp
and CrispASR. Start an update when the queue is idle. You can close the browser
while it builds. Both new binaries must pass their launch checks before jobs
use them. The terminal settings menu also supports offline updates.

**Settings → Stop desktop** closes the graphical session to free memory on a
systemd PC. Save your work first. For headless use, launch the studio from SSH
or a Linux text console before stopping the desktop. **Restore desktop** brings
back the graphical login. Both actions are also available in `bash settings.sh`.

Choose **Change web password** in the terminal menu to set a new password.

Everything else lives in **`internal/`**. Keep this folder with the app: it
contains the code, backend builds, settings, and saved jobs. Older settings and
jobs are preserved when upgrading.

[Help, password changes, and moving to another PC](internal/docs/OPERATIONS.md)
· [Developer notes](internal/docs/DEVELOPMENT.md)
