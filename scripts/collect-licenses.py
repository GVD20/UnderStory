"""Retain installed dependency notices for source and portable distributions."""
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
destination = root / 'docs' / 'licenses'
destination.mkdir(parents=True, exist_ok=True)
rows = []

def retain(files, target):
    copied = []
    for path, relative in files:
        if path.is_file():
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            copied.append(output.relative_to(destination).as_posix())
    return copied

for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
    name = distribution.metadata['Name']
    candidates = []
    for relative in distribution.files or []:
        if any(word in relative.name.lower() for word in ['license', 'licence', 'copying', 'copyright', 'notice']):
            candidates.append((Path(distribution.locate_file(relative)), Path(*relative.parts[1:])))
    copied = retain(candidates, destination / 'python' / name)
    rows.append({'ecosystem': 'Python', 'name': name, 'version': distribution.version,
                 'license': distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or '',
                 'files': copied})

lock = json.loads((root / 'package-lock.json').read_text(encoding='utf-8'))['packages']
pending = ['@icon-park/react', 'react', 'react-dom', 'react-markdown', 'remark-gfm']
visited = set()
while pending:
    name = pending.pop()
    if name in visited:
        continue
    visited.add(name)
    path = root / 'node_modules' / name
    if not (path / 'package.json').is_file():
        raise RuntimeError(f'Missing renderer dependency: {name}')
    package = json.loads((path / 'package.json').read_text(encoding='utf-8'))
    pending.extend(package.get('dependencies', {}))
    candidates = [(file, Path(file.name)) for file in path.iterdir()
                  if file.is_file() and any(word in file.name.lower() for word in ['license', 'licence', 'copying', 'copyright', 'notice'])]
    for folder in ['licenses', 'license']:
        if (path / folder).is_dir():
            candidates.extend((file, file.relative_to(path)) for file in (path / folder).rglob('*') if file.is_file())
    copied = retain(candidates, destination / 'npm' / name.replace('/', '__'))
    rows.append({'ecosystem': 'npm', 'name': name, 'version': package['version'],
                 'license': package.get('license', ''), 'files': copied})

python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    retain([(python_license, Path('LICENSE.txt'))], destination / 'python' / 'CPython')
(destination / 'dependencies.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
(destination / 'README.md').write_text(
    '# Dependency license texts\n\n'
    'Collected from installed Python distributions and the renderer dependency graph. '
    '`dependencies.json` records package versions, upstream license identifiers and retained files. '
    'Electron and Chromium notices remain at the root of the portable application.\n', encoding='utf-8')
print(f'Retained notices for {len(rows)} dependencies')
