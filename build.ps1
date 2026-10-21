$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Install the project dependencies in .venv before building."
}

Set-Location $PSScriptRoot
& $python -m PyInstaller --noconfirm --clean PhotoUploader.spec
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
