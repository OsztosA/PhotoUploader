@echo off
setlocal

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Install dependencies first: .venv\Scripts\pip install -r requirements-build.txt
    exit /b 1
)

"%PYTHON%" -m PyInstaller --noconfirm --clean "%~dp0PhotoUploader.spec"
exit /b %ERRORLEVEL%
