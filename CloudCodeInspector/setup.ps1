param([string]$Python = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Python) {
    $candidate = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $candidate) { $Python = $candidate }
}
if ($Python) { & $Python -m venv .venv } else { py -3.12 -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required. Install Python, then run this setup again.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .\.venv\Scripts\python.exe -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed.' }
Write-Host 'Ready. Open launch.cmd or run .venv\Scripts\python.exe run.py --help'
