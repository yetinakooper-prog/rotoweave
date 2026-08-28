@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start.ps1"
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" if not "%ROTOWEAVE_NO_PAUSE%"=="1" pause
exit /b %CODE%
