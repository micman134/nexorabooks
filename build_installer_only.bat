@echo off
REM ===================================================================
REM  Build ONLY the installer, from an application that is already built.
REM
REM  build_windows.bat does everything: tests, application, installer.
REM  That is the right script and takes about half an hour, most of it
REM  running the tests, which is time well spent when the code changed.
REM
REM  This one is for the case where the code did NOT change and the
REM  tests already passed - you have dist\NexoraBooks\NexoraBooks.exe
REM  sitting there and only the installer is missing. It skips straight
REM  to Inno Setup and finishes in under a minute.
REM
REM  It refuses to run if the application is not already built, so it
REM  can never quietly produce an installer for code nobody tested.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  Building the Nexora Books installer
echo  ==================================
echo.

if not exist "dist\NexoraBooks\NexoraBooks.exe" (
  echo  There is no built application to wrap up.
  echo.
  echo  Expected: dist\NexoraBooks\NexoraBooks.exe
  echo.
  echo  Run build_windows.bat first. That runs the tests and builds the
  echo  application; this script only makes the installer afterwards.
  pause
  exit /b 1
)

for /f "delims=" %%V in ('python version.py') do set APPVER=%%V
if "%APPVER%"=="" (
  echo  Could not read the version out of app\config.py. Nothing was built.
  pause
  exit /b 1
)
echo  Version: %APPVER%

set "ISCC="
set "PF32=%ProgramFiles(x86)%"
for %%R in ("%ProgramFiles%" "%PF32%" "%LocalAppData%\Programs") do (
  for /f "delims=" %%D in ('dir /b /ad /o-n "%%~R\Inno Setup*" 2^>nul') do (
    if not defined ISCC if exist "%%~R\%%D\ISCC.exe" set "ISCC=%%~R\%%D\ISCC.exe"
  )
)
if not defined ISCC (
  for /f "delims=" %%P in ('where ISCC.exe 2^>nul') do (
    if not defined ISCC set "ISCC=%%P"
  )
)
if not defined ISCC (
  for /f "tokens=2,*" %%A in (
    'reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\ISCC.exe" /ve 2^>nul ^| find "REG_"'
  ) do (
    if not defined ISCC if exist "%%B" set "ISCC=%%B"
  )
)

if not defined ISCC (
  echo.
  echo  Inno Setup was not found on this computer.
  echo  Get it free from https://jrsoftware.org/isdl.php and run this again.
  echo  Any version from 6 onwards works.
  pause
  exit /b 1
)

echo  Using Inno Setup at: %ISCC%
echo.
"%ISCC%" /Qp /DAppVersion=%APPVER% "installer\NexoraBooks.iss"
if errorlevel 1 (
  echo.
  echo  Inno Setup could not build the installer. The error is above.
  pause
  exit /b 1
)

echo.
echo  ===================================================================
echo   Done.
echo.
echo   Installer:  dist\NexoraBooks-%APPVER%-Setup.exe
echo.
echo   That single file is what a customer downloads and double-clicks.
echo   It is NOT signed, so Windows will say the publisher is unknown.
echo   See SELLING.md before you put it on your website.
echo  ===================================================================
echo.
pause
exit /b 0
