@echo off
REM Start the Statement Reader for everyday use (not development).
REM
REM No --reload: the reloader spawns a child process, which makes the app harder
REM to stop cleanly and restarts it on any file touch. It also holds the SQLite
REM file open in two processes.
REM
REM Bound to 127.0.0.1 on purpose. Tailscale proxies to localhost
REM (tailscale serve --bg 8000), so the app itself never listens on a public
REM interface. Do not change this to 0.0.0.0.
REM
REM Output goes to data\server.log so there is something to read when someone
REM reports "it did not work". data\ is gitignored.

cd /d "%~dp0.."

if not exist "data" mkdir "data"

echo. >> "data\server.log"
echo ===== started %DATE% %TIME% ===== >> "data\server.log"

REM Prefer whatever python is on PATH; fall back to the known install location,
REM because a shortcut launched at sign-in does not always inherit the same PATH
REM an interactive shell has.
set PY=python
where python >nul 2>&1 || set PY=C:\Python314\python.exe

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "data\server.log" 2>&1
