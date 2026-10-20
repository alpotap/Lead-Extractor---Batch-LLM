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

echo Python 3.11 or newer was not found. Installing Python 3.12 for this Windows user...
where winget >nul 2>&1
if not errorlevel 1 (
  winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if not defined PYTHON_EXE (
  echo winget was unavailable or could not install Python. Downloading the official Python installer...
  set "PY_INSTALLER=%TEMP%\lead-desk-python-3.12.10.exe"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%TEMP%\lead-desk-python-3.12.10.exe'; Start-Process -FilePath '%TEMP%\lead-desk-python-3.12.10.exe' -ArgumentList '/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1' -Wait"
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if not defined PYTHON_EXE (
  echo Could not find or install Python. Check your internet connection and try again.
  pause
  exit /b 1
)

:python_found
echo Using Python:
"%PYTHON_EXE%" %PYTHON_ARGS% --version

if not exist ".venv\Scripts\python.exe" (
  echo Creating the app's private Python environment...
  "%PYTHON_EXE%" %PYTHON_ARGS% -m venv .venv
  if errorlevel 1 goto :failed
)

echo Checking and installing app packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

where ollama >nul 2>&1
if errorlevel 1 (
  where winget >nul 2>&1
  if not errorlevel 1 (
    echo Ollama was not found. Installing it for this Windows user...
    winget install --id Ollama.Ollama --exact --scope user --silent --accept-package-agreements --accept-source-agreements
  )
)

set "OLLAMA_EXE="
for /f "delims=" %%P in ('where ollama.exe 2^>nul') do if not defined OLLAMA_EXE set "OLLAMA_EXE=%%P"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA_EXE (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -TimeoutSec 2 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if errorlevel 1 (
    echo Starting the local Ollama service in the background...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%OLLAMA_EXE%' -ArgumentList 'serve' -WindowStyle Hidden"
    timeout /t 3 /nobreak >nul
  )
) else (
  echo Ollama could not be installed automatically. The lead manager will still start, but AI processing needs Ollama.
)

echo Starting Lead Extractor. Keep this window open while using the app.
".venv\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Startup failed. The message above should explain what needs attention.
pause
exit /b 1
