@echo off
REM ===================================================================
REM  Starts Nexora Books from source, without building the .exe first.
REM  Handy while you are trying it out.
REM
REM  The first run installs the handful of libraries it needs. After
REM  that this starts straight away.
REM ===================================================================
cd /d "%~dp0"

REM --- Find Python. Fresh Windows machines answer to "py" but not
REM --- always to "python", so try both before giving up.
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" where py >nul 2>nul && set PY=py
if "%PY%"=="" (
  echo.
  echo  Python is not installed on this computer, or it was installed
  echo  without ticking "Add python.exe to PATH".
  echo.
  echo  Get it from  https://www.python.org/downloads/  and tick that box
  echo  on the first screen of the installer, then run this again.
  echo.
  pause
  exit /b 1
)

REM --- Are the libraries there? If not, install them, once.
%PY% -c "import fastapi, uvicorn, sqlalchemy, jinja2, multipart, itsdangerous" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  First run — installing what Nexora Books needs. This takes a
  echo  minute and only happens once.
  echo.
  %PY% -m pip install --upgrade pip --quiet
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  That did not work. The messages above say why.
    echo  Send them to whoever supports Nexora Books.
    echo.
    pause
    exit /b 1
  )
  echo.
  echo  Done. Starting Nexora Books...
  echo.
)

%PY% run.py
if errorlevel 1 pause
