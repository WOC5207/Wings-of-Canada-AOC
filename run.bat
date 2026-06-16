@echo off
rem Wings of Canada AOC launcher. First run creates the environment.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up the Python environment - one-time step...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet flask waitress || goto :error
)

".venv\Scripts\python.exe" app.py
goto :eof

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on your PATH.
pause
