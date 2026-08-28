@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "ROLE=%~1"
if "%ROLE%"=="" set "ROLE=Client"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-RotoWeave.ps1" -Role "%ROLE%"
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" if not "%ROTOWEAVE_NO_PAUSE%"=="1" pause
exit /b %CODE%
