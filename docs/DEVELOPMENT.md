# Development

Edit `app/app.py`, `app/index.html`, `app/style.css`, and `app/script.js` directly.
The app uses only Python's standard library. Runtime data is in `data/`; static
assets are served from `app/`. Do not embed application source in an installer.

- `setup.sh`: dependency/GPU checks, migration, build, configure, start, self-delete.
- `scripts/dependencies.sh`: distro detection, complete dependency report and optional installation.
- `scripts/build-backends.sh`: official checkouts, Vulkan Release builds and native tuning.
- `scripts/hardware.py`: affinity, physical cores, available RAM/cgroup build defaults.
- `scripts/configure.py`: migrate legacy data and write private relative-path configuration.
- `scripts/service.py`: PID ownership checks, start readiness and graceful stop.
- `scripts/run-model.py`: portable CLI and translation server launchers.
- `tools/package.py`: explicit source-only archive allowlist, excluding private files.

Run checks from the project root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app scripts tools tests
bash -n setup.sh scripts/dependencies.sh scripts/build-backends.sh web-start.sh web-stop.sh run.sh translate.sh change-web-password.sh
```

Tests use temporary projects and fake Git/CMake/Vulkan executables. They exercise
setup completion/failure, no-model installation, migration, relative paths after
moving, cache rebuilding, dependency reporting and package mappings without network,
model downloads, or GPU access. App regressions also cover model validation,
backend launch arguments, static assets, saved-job recovery, and shutdown command
selection. Actual dependency installation and Vulkan inference need testing on
each target distro with its GPU drivers and real models.

Create releases from fresh source containing `setup.sh` using
`python3 tools/package.py`; inspect `dist/local-ai-studio.tar.gz`. Do not distribute
`data/`, logs, models, or backend checkouts. Keep auth, CSRF, upload limits,
subtitle marker validation, and Range support when editing the existing app.
