@echo off
REM JMReader launcher (Windows).
REM All dependencies go into the project-local .venv, never into global Python.
REM
REM NOTE: this file is deliberately ASCII-only.
REM   A .bat holding multi-byte characters (Chinese) with LF-only line endings
REM   desyncs the cmd.exe parser: it spins on WinError 123 and the window
REM   closes instantly. ASCII + CRLF is the only combination that is safe
REM   regardless of how the file was copied onto the machine.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

REM Self-check: a .bat that reached this machine through a copy instead of a
REM git checkout may have LF endings. cmd can mis-parse that, and the usual
REM symptom is a window that opens and closes instantly with no explanation.
REM The parser is already confused by then, so we cannot reliably warn from
REM inside -- instead just make sure we never exit silently: every failure
REM path below ends in pause.
if not exist "app\main.py" goto :wrongdir

REM "venv works" is not the same as "the interpreter exists". A pip run that
REM died halfway, or a .venv copied from someone else, leaves an environment
REM that starts but lacks packages -- the resulting ModuleNotFoundError points
REM nowhere near the install step. So let the interpreter import the real
REM dependencies; only then is it considered installed.
if not exist "%PY%" goto :create
"%PY%" -c "import fastapi, uvicorn, jmcomic" >nul 2>&1
if not errorlevel 1 goto :run

echo.
echo [1/2] .venv exists but is incomplete; installing missing packages ...
"%PY%" -m pip install -r requirements.txt || goto :fail
goto :run

:create
echo.
echo [1/2] Creating virtual environment .venv ...
python -m venv .venv || goto :nopython
echo [2/2] Installing dependencies (into .venv only) ...
"%PY%" -m pip install --upgrade pip || goto :fail
"%PY%" -m pip install -r requirements.txt || goto :fail

:run
REM Keep the window open when the app exits non-zero, so the traceback stays
REM readable instead of vanishing together with the window.
"%PY%" -m app
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :crashed
goto :eof

:crashed
echo.
echo ================================================================
echo  JMReader exited with code %RC%.
echo  The error message is printed above this box.
echo ================================================================
echo.
pause
exit /b %RC%

:wrongdir
echo.
echo Cannot find app\main.py next to this script.
echo Run run.bat from inside the JMReader folder.
echo.
pause
exit /b 1

:nopython
echo.
echo Could not create the virtual environment. Please check:
echo   1. Python 3.10 or newer is installed (https://www.python.org/downloads/)
echo   2. "Add python.exe to PATH" was ticked during installation
echo   3. Running "python -V" in a terminal prints a version
echo.
pause
exit /b 1

:fail
echo.
echo Dependency installation failed. Check that PyPI is reachable.
echo Everything is installed into the project .venv, your global Python
echo environment is untouched, so it is safe to simply retry.
echo.
pause
exit /b 1
