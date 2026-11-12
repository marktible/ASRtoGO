# Development

Keep the root focused on setup, start/stop, settings, and models. All implementation and
maintenance files live under `internal/`:

- `app/`: Python HTTP server and browser HTML/CSS/JS.
- `scripts/`: dependency checks, Vulkan builds, migration, service management,
  hardware detection, and password changes.
- `backends/`: generated backend checkouts and builds (ignored by Git).
- `data/`: private settings, jobs, logs, and PID (ignored by Git).
- `docs/`, `tests/`, `tools/`: technical reference, tests, and release packaging.

Project paths and internal model IDs are relative to the repository root.
Setup and start recognize older root-level `data/`, `.plamo-web/`, and backend
folders. Migration preserves credentials/jobs and refuses ambiguous merges.
The web app owns the model servers; standalone inference launchers are omitted.
`settings.sh` opens `internal/scripts/settings.py`. Add future menu entries to
its `ACTIONS` mapping. Password changes preserve other settings and restart only
this project's running web app.

Run from the repository root:

```bash
python3 -m unittest discover -s internal/tests -v
python3 -m compileall -q internal/app internal/scripts internal/tools internal/tests
bash -n setup.sh start.sh stop.sh settings.sh internal/scripts/*.sh
```

Tests exercise setup success/failure, Vulkan/native build options, dependency
reports and distro mappings, saved-job migration, relative paths after moving,
start/stop readiness, model validation, and clean archive contents. Temporary
projects and fake Git/CMake/Vulkan tools avoid downloads and GPU dependencies;
real distro installation and model inference still require host validation.

Before setup deletes itself, create a source release with:

```bash
python3 internal/tools/package.py
```

The archive is `internal/dist/ASRtoGO.tar.gz`. It includes the license, code, model
folder instructions, and user entry points. It excludes runtime data, backend
checkouts, model weights, logs, bytecode, and Git metadata.

Preserve auth, CSRF, upload limits, subtitle marker validation, and audio Range
support when editing the app. See [OPERATIONS.md](OPERATIONS.md) for maintenance,
[ARCHITECTURE.md](ARCHITECTURE.md) for data flow, and [API.md](API.md) for routes.
