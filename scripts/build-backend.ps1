$ErrorActionPreference = 'Stop'
uv run python scripts/collect-licenses.py
if ($LASTEXITCODE -ne 0) { throw 'Dependency license collection failed' }
uv run pyinstaller --noconfirm --onedir --name understory-backend --paths backend --distpath build --workpath build/pyinstaller backend/run.py
if ($LASTEXITCODE -ne 0) { throw 'Backend packaging failed' }
