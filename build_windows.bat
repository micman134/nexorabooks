@echo off
REM ===================================================================
REM  Build Nexora Books into a Windows application and an installer.
REM
REM  Run this on a Windows computer with Python 3.11 or newer. It makes
REM     dist\NexoraBooks\NexoraBooks.exe        the application
REM     dist\NexoraBooks-<version>-Setup.exe    the installer, if Inno
REM                                             Setup is installed
REM
REM  The version is read from app\config.py, so it is never out of step
REM  with what the software reports about itself.
REM
REM  SIGNING: put your code-signing certificate's thumbprint in
REM  SIGN_THUMBPRINT below and both the application and the installer are
REM  signed. Without it Windows tells the first person who runs it that
REM  the publisher is unknown, and an unsigned application cannot carry
REM  its reputation from one version to the next — every update starts
REM  that warning again. See SELLING.md.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- Your code-signing certificate, when you have one ----------------
set SIGN_THUMBPRINT=
set TIMESTAMP_URL=http://timestamp.digicert.com

echo.
echo  Building Nexora Books for Windows
echo  =================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  Python was not found.
  echo  Install Python 3.11 or newer from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" on the first screen of the
  echo  installer, then close this window, open a new one, and try again.
  pause
  exit /b 1
)

REM --- Which version are we building? ----------------------------------
for /f "delims=" %%V in ('python version.py') do set APPVER=%%V
if "%APPVER%"=="" (
  echo  Could not read the version out of app\config.py. Nothing was built.
  pause
  exit /b 1
)
echo  Version to build: %APPVER%
echo.

echo  [1/6] Creating a clean build environment...
if exist build_env rmdir /s /q build_env
python -m venv build_env
if errorlevel 1 goto :failed

call build_env\Scripts\activate.bat

echo  [2/6] Installing what Nexora Books needs...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if errorlevel 1 goto :failed

echo  [3/6] Installing the packaging tool...
python -m pip install pyinstaller --quiet
if errorlevel 1 goto :failed

REM --- The native desktop window is a convenience, not a requirement --------
REM pywebview needs pieces of Windows that are not on every machine, and it
REM lags behind new Python releases. If it will not install, Nexora Books opens
REM in the default browser instead — the same program, the same books, one
REM window that says "Chrome" at the top. Letting that stop the whole build
REM would be losing a working product over a cosmetic one.
echo        Installing the optional desktop window...
python -m pip install pywebview --quiet
if errorlevel 1 (
  echo.
  echo        The desktop window could not be installed on this machine.
  echo        Carrying on: the application will open in your browser instead.
  echo        Everything else is unaffected.
  echo.
)

echo  [4/6] Running the tests on THIS computer before packaging anything...
echo        Windows is where the last several bugs came from, so this runs
echo        every time. Windows may ask to allow Python through the firewall;
echo        allow it for private networks. Some tests start a small web
echo        server briefly.
echo.
python -m pip install pytest httpx --quiet

REM --- Use every core this machine has -----------------------------------
REM The tests are the slow part of this build, not the packaging. Run one
REM per core and the wait roughly halves on two cores and falls further on
REM more. "--dist loadfile" keeps every test in a file on the same worker,
REM which matters because tests in a file share a temporary data folder;
REM splitting them across workers would make them fail for reasons that have
REM nothing to do with the software.
REM
REM If pytest-xdist will not install, the tests still run - just serially.
set PYTEST_SPEED=
python -m pip install pytest-xdist --quiet
if not errorlevel 1 (
  set PYTEST_SPEED=-n auto --dist loadfile
  echo        Running them in parallel, one per processor core.
) else (
  echo        Running them one at a time; this takes about twenty-five minutes.
)
echo.
python -m pytest tests -q %PYTEST_SPEED%
if errorlevel 1 (
  echo.
  echo  The tests did not pass on this computer. NOTHING has been packaged.
  echo.
  echo  Do not work around this. A test that passes elsewhere and fails
  echo  here has found a real difference between Windows and everything
  echo  else, which is exactly what it is for. Copy the failures above and
  echo  send them on.
  pause
  exit /b 1
)

echo  [5/6] Packaging the application...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
pyinstaller NexoraBooks.spec --noconfirm --clean
if errorlevel 1 goto :failed

if not exist "dist\NexoraBooks\NexoraBooks.exe" (
  echo  Packaging finished but dist\NexoraBooks\NexoraBooks.exe is not there.
  echo  Nothing further was done.
  pause
  exit /b 1
)

if not "%SIGN_THUMBPRINT%"=="" (
  echo        Signing the application...
  signtool sign /sha1 %SIGN_THUMBPRINT% /fd sha256 /tr %TIMESTAMP_URL% /td sha256 ^
    "dist\NexoraBooks\NexoraBooks.exe"
  if errorlevel 1 (
    echo  Signing failed. The application is built but unsigned.
    echo  Check that signtool is on your PATH ^(it comes with the Windows SDK^)
    echo  and that the thumbprint matches a certificate in your store.
  )
)

echo  [6/6] Building the installer...
REM --- Finding Inno Setup, whichever version is installed ---------------
REM This used to name "Inno Setup 6" three times, so somebody who installed
REM version 7 was told Inno Setup "was not found" while it sat on their
REM machine. Any folder called "Inno Setup <something>" now counts, newest
REM first, and if it is somewhere unusual the PATH and the registry are asked.
set "ISCC="
REM Copied out first: a name with brackets in it inside a bracketed block is
REM one of the ways a batch file quietly stops doing what it says.
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
if defined ISCC echo        Using Inno Setup at: %ISCC%

if not defined ISCC (
  echo.
  echo        Inno Setup was not found, so no installer was made.
  echo        The application in dist\NexoraBooks\ works perfectly well -
  echo        copy the whole folder to any Windows computer and run it.
  echo.
  echo        To make a proper installer, get Inno Setup ^(free^) from
  echo            https://jrsoftware.org/isdl.php
  echo        and run this script again.
) else (
  "%ISCC%" /Qp /DAppVersion=%APPVER% "installer\NexoraBooks.iss"
  if errorlevel 1 goto :failed
  if not "%SIGN_THUMBPRINT%"=="" (
    echo        Signing the installer...
    signtool sign /sha1 %SIGN_THUMBPRINT% /fd sha256 /tr %TIMESTAMP_URL% /td sha256 ^
      "dist\NexoraBooks-%APPVER%-Setup.exe"
  )
)

echo.
echo  ===================================================================
echo   Done.
echo.
echo   Application:  dist\NexoraBooks\NexoraBooks.exe
if defined ISCC echo   Installer:    dist\NexoraBooks-%APPVER%-Setup.exe
echo.
if "%SIGN_THUMBPRINT%"=="" (
  echo   NOT SIGNED. Windows will warn the first person who runs it that
  echo   the publisher is unknown. Fine for testing; see SELLING.md before
  echo   you sell copies.
)
echo  ===================================================================
echo.
pause
exit /b 0

:failed
echo.
echo  The build failed. The error is above.
pause
exit /b 1
