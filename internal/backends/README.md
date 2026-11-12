# Backends

Setup clones llama.cpp and CrispASR here and builds each in `build-studio/`.
Only the llama-server and crispasr targets are needed by the web app.

Rebuild after moving to another machine or distribution (from the project root):

```bash
bash internal/scripts/dependencies.sh --check
bash internal/scripts/build-backends.sh
python3 internal/scripts/configure.py
```

Generated caches are rebuilt when paths, CPU features, distro, or compiler change.
Existing source checkouts are preserved without automatic pull/reset. Optional
`LLAMA_REF` and `CRISPASR_REF` select existing tags or commits.
