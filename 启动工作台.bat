@echo off
rem A-share web workbench launcher for Windows
rem Use system Python when available; otherwise install/use uv.
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if not errorlevel 1 goto :run_system_python

where uv >nul 2>nul
if not errorlevel 1 goto :run_uv

echo [launcher] Python and uv were not found. Installing uv...
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo [launcher] Failed to install uv.
    exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
goto :run_uv

:run_system_python
python daily-stock-analysis/scripts/web_workbench.py %*
exit /b %errorlevel%

:run_uv
uv run --python 3.13 --with requests --with pyyaml --with tzdata python daily-stock-analysis/scripts/web_workbench.py %*
exit /b %errorlevel%
