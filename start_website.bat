@echo off
rem Starts the SigmaAgent production website at the agreed local address.
rem Run npm run build in the website folder before the first start or after code changes.

cd /d "%~dp0website"
start "SigmaAgent Website (:8080)" cmd /k "npm run start -- --hostname 127.0.0.1 --port 8080"

echo Website starting on http://localhost:8080
