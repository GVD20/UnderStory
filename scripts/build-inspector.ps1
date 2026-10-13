$ErrorActionPreference = 'Stop'
uv run pyinstaller --noconfirm --onedir --windowed --name UnderStory-Inspector --distpath build/tools --workpath build/inspector tools/inspect.py
if ($LASTEXITCODE -ne 0) { throw 'Inspector packaging failed' }
