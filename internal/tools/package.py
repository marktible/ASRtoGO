#!/usr/bin/env python3
"""Create a clean source archive with the same simple user-facing layout."""
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[2]
output = root / 'internal/dist/ASRtoGO.tar.gz'
output.parent.mkdir(exist_ok=True)
if not (root / 'setup.sh').is_file():
    raise SystemExit('setup.sh has already removed itself. Create release archives from a fresh source checkout.')
with tarfile.open(output, 'w:gz') as archive:
    for name in ('README.md', 'LICENSE', 'setup.sh', 'start.sh', 'stop.sh', 'settings.sh', '.gitignore'):
        archive.add(root / name, arcname=f'ASRtoGO/{name}')
    for folder in ('app', 'scripts', 'docs', 'tests', 'tools'):
        for path in sorted((root / 'internal' / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                archive.add(path, arcname=f'ASRtoGO/{path.relative_to(root)}')
    for name in ('internal/backends/README.md', 'models/translation/README.md', 'models/asr/README.md'):
        archive.add(root / name, arcname=f'ASRtoGO/{name}')
print(output)
