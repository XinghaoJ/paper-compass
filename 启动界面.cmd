@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv_local\Scripts\python.exe" (
    ".venv_local\Scripts\python.exe" 0web_ui.py
) else (
    python 0web_ui.py
)
pause
