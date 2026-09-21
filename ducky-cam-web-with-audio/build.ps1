param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $env:PYTHONUSERBASE = Join-Path $PSScriptRoot '.build\python-user'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot '.build\cache'
    & $Python -m PyInstaller --noconfirm --onefile --windowed --hidden-import pystray._win32 --name webcam_server --distpath dist --workpath .build\server --specpath .build --add-binary "${PSScriptRoot}\cloudflared.exe;." --add-data "${PSScriptRoot}\page.html;." webcam_server.py
    if ($LASTEXITCODE) { throw 'Server build failed' }
    & $Python -m PyInstaller --noconfirm --onefile --collect-all imageio_ffmpeg --name viewer --distpath dist --workpath .build\viewer --specpath .build viewer.py
    if ($LASTEXITCODE) { throw 'Viewer build failed' }
    & $Python -m PyInstaller --noconfirm --onefile --collect-all imageio_ffmpeg --name CheckConnection --distpath dist --workpath .build\check --specpath .build --add-binary "${PSScriptRoot}\cloudflared.exe;." --add-data "${PSScriptRoot}\page.html;." check_connection.py
    if ($LASTEXITCODE) { throw 'Diagnostic build failed' }
} finally { Pop-Location }
