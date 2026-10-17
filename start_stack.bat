@echo off
rem Starts the full SigmaAgent stack in three separate windows:
rem   1. main investigation API on port 8000
rem   2. context retrieval API on port 8001
rem   3. the Discord bot (single-instance lock applies)
rem Run this from the sigmaAgent folder (double-click works too).

cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist "C:\Python312\python.exe" set "PYTHON_EXE=C:\Python312\python.exe"

start "SigmaAgent API (8000)" cmd /k %PYTHON_EXE% -u -m uvicorn api:app --host 127.0.0.1 --port 8000

start "SigmaAgent Context API (8001)" cmd /k %PYTHON_EXE% -u -m uvicorn api:app --host 127.0.0.1 --port 8001 --app-dir "context retrieval"

start "SigmaAgent Discord Bot" /D "%~dp0discord-bot" cmd /k .venv\Scripts\python.exe bot.py

echo SigmaAgent stack started: API (8000), Context API (8001), Discord bot.
echo Close each window (or Ctrl+C inside it) to stop that component.
