# Compiled backends

Setup clones the official llama.cpp and CrispASR repositories into this folder
and builds each in `build-studio/`. These checkouts are dependencies; app code
lives in `app/`.

Rebuild after moving the project to another machine or Linux distribution:

```bash
bash scripts/dependencies.sh --check
bash scripts/build-backends.sh
python3 scripts/configure.py
```

The rebuild detects changed project locations and CPU features and discards only
its generated `build-studio/` cache when necessary. It preserves source checkouts,
models, passwords, and jobs. Existing checkouts are never pulled or reset
implicitly. Set `LLAMA_REF` and `CRISPASR_REF` to local tags/commits to build
specific versions.
