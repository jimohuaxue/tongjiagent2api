@echo off
setlocal

cd /d "%~dp0"

if "%WEB2API_CONFIG_PATH%"=="" set "WEB2API_CONFIG_PATH=config.local.yaml"
if not exist "%WEB2API_CONFIG_PATH%" (
  copy config.yaml "%WEB2API_CONFIG_PATH%" >nul
  echo Created %WEB2API_CONFIG_PATH% from config.yaml.
)

if "%WEB2API_DB_PATH%"=="" set "WEB2API_DB_PATH=db.sqlite3"

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
  uv sync || goto :error
  echo Starting Web2API at http://127.0.0.1:9000
  uv run python main.py
  goto :end
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PY_CMD=py -3.12"
) else (
  where python >nul 2>nul
  if not %ERRORLEVEL%==0 (
    echo Python 3.12+ or uv is required.
    goto :error
  )
  set "PY_CMD=python"
)

if not exist ".venv\Scripts\python.exe" (
  %PY_CMD% -m venv .venv || goto :error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -e . || goto :error

echo Starting Web2API at http://127.0.0.1:9000
".venv\Scripts\python.exe" main.py
goto :end

:error
echo Failed to start Web2API.
exit /b 1

:end
endlocal
