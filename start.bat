@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0llmirror.exe" goto source
"%~dp0llmirror.exe" %*
set "LLMIRROR_RESULT=%ERRORLEVEL%"
if not "%LLMIRROR_RESULT%"=="0" pause
exit /b %LLMIRROR_RESULT%

:source
set "LLMIRROR_PYTHON=python"
where py >nul 2>nul
if not errorlevel 1 set "LLMIRROR_PYTHON=py -3"
%LLMIRROR_PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto python_missing
if exist ".venv\Scripts\python.exe" goto dependencies
%LLMIRROR_PYTHON% -m venv .venv
if errorlevel 1 goto failed
:dependencies
fc /b requirements.txt ".venv\llmirror-requirements.txt" >nul 2>nul
if not errorlevel 1 goto run_source
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed
copy /y requirements.txt ".venv\llmirror-requirements.txt" >nul
:run_source
".venv\Scripts\python.exe" launcher.py %*
set "LLMIRROR_RESULT=%ERRORLEVEL%"
if not "%LLMIRROR_RESULT%"=="0" pause
exit /b %LLMIRROR_RESULT%
:python_missing
echo Python 3.11+ is required when llmirror.exe is unavailable.
:failed
pause
exit /b 1
