@echo off
rem cwf —— ComfyUI workflow read / compose / layout toolkit (Windows launcher)
rem Usage:  cwf <command> [options]      e.g.  cwf ws list --dir H3
rem
rem Python resolution order:
rem   1. %CWF_PYTHON%          (set it to force a specific interpreter)
rem   2. .venv\Scripts\python.exe next to this script
rem   3. python on PATH
setlocal
set "CWF_HOME=%~dp0"
set "PY=%CWF_PYTHON%"
if not defined PY (
  if exist "%CWF_HOME%.venv\Scripts\python.exe" (
    set "PY=%CWF_HOME%.venv\Scripts\python.exe"
  ) else (
    set "PY=python"
  )
)
"%PY%" -X utf8 "%CWF_HOME%cwf_run.py" %*
exit /b %ERRORLEVEL%
