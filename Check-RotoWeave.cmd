@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "ROLE=%~1"
if "%ROLE%"=="" set "ROLE=Client"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Check-RotoWeave.ps1" -Role "%ROLE%"
exit /b %ERRORLEVEL%
