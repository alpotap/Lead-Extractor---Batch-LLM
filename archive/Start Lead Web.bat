@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
set "PYTHON_ARGS="

rem Prefer the Python launcher, and explicitly select an installed supported version.
for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY_LAUNCHER set "PY_LAUNCHER=%%P"
if defined PY_LAUNCHER (
  for %%V in (3.14 3.13 3.12 3.11) do (
    "%PY_LAUNCHER%" -%%V -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_EXE=%PY_LAUNCHER%"
      set "PYTHON_ARGS=-%%V"
      goto :python_found
    )
  )
)

rem Search PATH entries, ignoring the Windows Store execution alias.
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
  echo %%P| findstr /i /c:"\WindowsApps\" >nul
  if errorlevel 1 (
    "%%P" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_EXE=%%P"
      goto :python_found
    )
  )
)

rem Search standard per-user and system installation locations.
for %%V in (314 313 312 311) do (
  if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    goto :python_found
  )
  if exist "%ProgramFiles%\Python%%V\python.exe" (
    set "PYTHON_EXE=%ProgramFiles%\Python%%V\python.exe"
    goto :python_found
  )
  if exist "%ProgramFiles(x86)%\Python%%V\python.exe" (
    set "PYTHON_EXE=%ProgramFiles(x86)%\Python%%V\python.exe"
    goto :python_found
  )
)

echo Python 3.11 or newer was not found. Please install Python 3.11 or 3.12.
pause
exit /b 1

:python_found
if not exist ".venv\Scripts\python.exe" (
  echo Creating the app's private Python environment...
  "%PYTHON_EXE%" %PYTHON_ARGS% -m venv .venv
  if errorlevel 1 goto :failed
)

echo Checking and installing app packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

where ollama >nul 2>&1
if not errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -TimeoutSec 2 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if errorlevel 1 (
    echo Starting the local Ollama service in the background...
    start "" /b ollama serve
    timeout /t 2 /nobreak >nul
  )
)

echo.
echo ========================================================
echo   Lead Extractor Web Desk (FastAPI + Modern Web UI)
echo   Open your browser at: http://localhost:8502
echo ========================================================
echo.

start "" "http://localhost:8502"
".venv\Scripts\python.exe" server.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Startup failed.
pause
exit /b 1
