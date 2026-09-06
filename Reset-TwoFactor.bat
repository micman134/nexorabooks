@echo off
REM ===================================================================
REM  Locked out by two-factor sign-in?
REM
REM  Close Nexora Books first, then run this. It asks which company and
REM  which person, turns the second step off for them, and writes what
REM  it did to that company's audit trail.
REM
REM  Nothing else is touched: passwords, invoices and everything else
REM  stay exactly as they are.
REM ===================================================================
cd /d "%~dp0"

set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" where py >nul 2>nul && set PY=py
if "%PY%"=="" (
  echo.
  echo  Python is not installed on this computer, or it was installed
  echo  without ticking "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

"%PY%" reset_two_factor.py %*
echo.
pause
