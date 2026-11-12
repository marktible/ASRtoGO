# Local AI Studio

Local translation, speech recognition, and subtitle translation in a mobile web
interface. Uses llama.cpp and CrispASR with Vulkan. Python standard library and
plain HTML/CSS/JavaScript; no pip, Node, systemd service, or cloud API is required.

## Setup

Extract the project into any writable directory and run as your regular user:

```bash
bash setup.sh
```

Setup checks dependencies and a usable Vulkan device, clones both backends,
builds them for this machine, preserves any existing password and saved jobs,
and starts the web interface. First installation asks for a password in the
terminal. **After success, setup.sh deletes itself.** On an error it remains so
you can fix the reported issue and retry. `--no-start` finishes setup without
starting the website.

To let setup install missing packages using your distro's package manager:

```bash
bash setup.sh --install-deps
```

Administrator access is used only for package installation through sudo/doas.
You can also check/install dependencies independently:

```bash
bash internal/scripts/dependencies.sh --check
bash internal/scripts/dependencies.sh --install
```

Dependency mappings cover Void (XBPS), Debian/Ubuntu (APT), Fedora and compatible
distributions (DNF), Arch/Manjaro (pacman), openSUSE (zypper), and Alpine (apk).
These mappings are not a claim that every distro release has been tested;
older releases may need newer Python/CMake or additional repositories. Other
Linux distributions can install the reported tools manually. Alpine/minimal
images need Bash installed before running the scripts.

Required: Bash, Python >= 3.10, CMake >= 3.24, C/C++ compiler and make, Git,
pkg-config, FFmpeg, Vulkan headers/loader, glslc, vulkaninfo, and a Vulkan driver
for your GPU. Ninja and ccache are used when available. Setup does not change GPU
drivers. `vulkaninfo --summary` must report a GPU; software-only Vulkan devices
are rejected. No display server is required for the app.

## Add your own models

Models are **never downloaded by setup**. Installation works with empty model
folders. Download compatible models yourself and put them in:

| Task | Folder | Format |
| --- | --- | --- |
| Translation | `models/translation/` | llama.cpp translation `.gguf` |
| Speech recognition | `models/asr/` | CrispASR ASR `.gguf`, Whisper `.bin` |

Refresh Settings to see new models. Keep required companion files alongside the
model. Existing PLaMo translation prompts use source/target chat-template kwargs;
other model templates may require changes. ASR model architectures differ in
Vulkan support. See the [llama.cpp build guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)
and [CrispASR documentation](https://github.com/CrispStrobe/CrispASR) for backend/model support.

## Run

```bash
bash start.sh
bash stop.sh
bash settings.sh
```

The start script prints localhost and available LAN addresses. Web port defaults
to 8765; llama-server uses 8080 and CrispASR 8178 on loopback. Ports can be changed
in `internal/data/config.json` while stopped. Start/stop scripts work with runit, systemd,
OpenRC, and other init systems because they manage their own processes.

Translate text/TXT/SRT/VTT, transcribe audio/video, or transcribe and translate
subtitles. Upload must finish and appear in Jobs before closing the browser.
Jobs continue while the PC and app are running. Saved inputs, audio previews,
transcripts, and results remain until that job is deleted. Subtitle translation
preserves timestamps and retries smaller batches when needed.

Optional PC shutdown uses the system's existing loginctl/systemctl authorization,
or a previously configured restricted sudo rule. Setup does not create passwordless
shutdown privileges. A distribution's desktop/session policy may deny the action;
translation and ASR do not depend on shutdown permissions.

## Layout and moving to another machine

```text
ASRtoGO/
├── setup.sh                 # removed after successful setup
├── start.sh / stop.sh
├── settings.sh              # password changes and future settings
├── models/
│   ├── translation/
│   └── asr/
└── internal/
    ├── app/                 # application source and browser assets
    ├── scripts/             # dependency, build, password and service helpers
    ├── backends/            # llama.cpp and CrispASR checkouts/builds
    ├── data/                # private config, jobs, logs and PID
    ├── docs/ / tests/
    └── tools/               # source archive builder
```

Project paths and internal model IDs are relative. Stop the app before moving the
whole folder. Native CPU instructions and Linux libraries differ between machines,
so rebuild the backends on the destination:

```bash
bash internal/scripts/dependencies.sh --check
bash internal/scripts/build-backends.sh
python3 internal/scripts/configure.py
bash start.sh
```

Builds use Release optimization, `GGML_NATIVE=ON`, `GGML_VULKAN=ON`, and LTO where
the compiler supports it. Build workers are limited by available CPU affinity and
memory; inference defaults to available physical cores. `threads: 0` in config
auto-detects at launch; set a positive integer to override. These are practical
defaults, not a benchmark guarantee of the fastest settings for every model/GPU.

```bash
BUILD_JOBS=4 bash internal/scripts/build-backends.sh
GGML_LTO=OFF bash internal/scripts/build-backends.sh
```

Rebuilds reset only generated `build-studio/` caches when the project location or
CPU features change. Existing dependency checkouts are preserved without automatic
pull/reset. `LLAMA_REF` and `CRISPASR_REF` can select existing tags/commits.

Previous `data/` and `.plamo-web/` installations migrate into `internal/data/`. Root GGUFs move
into the matching model folders, and the old `llama.cpp/` checkout moves into
`internal/backends/`. Passwords, ports, jobs, and custom settings are preserved. Previously
configured models outside the project stay in place; move them into the model
folders yourself for portability. Do not merge two populated runtime directories
without a backup.

## Troubleshooting and privacy

Check `internal/data/web.log`, `internal/data/llm.log`, and `internal/data/asr.log`. Build errors remain visible
in the terminal; binary launch checks write `internal/data/*-check.log`. Missing GPU devices
usually mean the GPU driver, container device access, or Vulkan ICD needs attention.

Use a trusted LAN or private authenticated tunnel. HTTP has no built-in TLS;
do not port-forward the web app. Keep `internal/data/` private: it contains password hashes,
user uploads, transcripts, and logs. Models and backend builds are excluded from
source packages. To create a clean shareable archive before setup removes itself:

```bash
python3 internal/tools/package.py
```

Development and validation: [DEVELOPMENT.md](DEVELOPMENT.md).
