@echo off
rem Job Aggregator installer for Windows: double-click me (or run from cmd).
rem Runs install.ps1 with ExecutionPolicy Bypass so no system setting is changed.
rem Extra arguments are passed through, e.g.  install.bat -Uninstall
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Installer finished with errors (code %RC%). Scroll up for details.
pause
exit /b %RC%
