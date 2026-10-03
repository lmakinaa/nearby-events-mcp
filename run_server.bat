@echo off
rem Launcher wrapper for Claude Desktop (Windows). Creates .venv on first run, then starts the server.
rem Claude Desktop config:
rem   "nearby-events": { "command": "C:\\ABSOLUTE\\PATH\\nearby-events-mcp\\run_server.bat",
rem                   "env": { "NEARBY_EVENTS_DEFAULT_LOCATION": "Orchard Road, Singapore" } }
setlocal
set "DIR=%~dp0"
set "VENV=%DIR%.venv"
if not exist "%VENV%\Scripts\python.exe" (
  python -m venv "%VENV%" 1>&2
  "%VENV%\Scripts\python.exe" -m pip install --quiet -e "%DIR%" 1>&2
)
"%VENV%\Scripts\python.exe" -m nearby_events_mcp
