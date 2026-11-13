@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_reducto.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Reducto setup finished.
) else (
  echo Reducto setup did not finish. Review the message above.
)
pause
exit /b %EXIT_CODE%
