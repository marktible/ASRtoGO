#!/usr/bin/env python3
"""Create a clean, portable source archive without models or private data."""
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parent.parent
output = root / 'dist/local-ai-studio.tar.gz'
output.parent.mkdir(exist_ok=True)
if not (root / 'setup.sh').is_file():
    raise SystemExit('setup.sh has already removed itself. Create release archives from a fresh source checkout.')
with tarfile.open(output, 'w:gz') as archive:
    for name in ('README.md', 'setup.sh', 'web-start.sh', 'web-stop.sh', 'change-web-password.sh', 'run.sh', 'translate.sh', '.gitignore'):
        archive.add(root / name, arcname=f'local-ai-studio/{name}')
    for folder in ('app', 'scripts', 'docs', 'tests', 'tools'):
        for path in sorted((root / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                archive.add(path, arcname=f'local-ai-studio/{path.relative_to(root)}')
    for name in ('backends/README.md', 'models/translation/README.md', 'models/asr/README.md'):
        archive.add(root / name, arcname=f'local-ai-studio/{name}')
print(output)
