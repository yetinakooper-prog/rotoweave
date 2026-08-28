@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "ROLE=%~1"
if "%ROLE%"=="" set "ROLE=Client"
if "%~2"=="" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup-RotoWeave.ps1" -Role "%ROLE%"
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup-RotoWeave.ps1" -Role "%ROLE%" -BundlePath "%~2" -ExpectedBundleSha256 "%~3"
)
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" if not "%ROTOWEAVE_NO_PAUSE%"=="1" pause
exit /b %CODE%
