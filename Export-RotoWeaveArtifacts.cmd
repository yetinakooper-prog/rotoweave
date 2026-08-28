@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: Export-RotoWeaveArtifacts.cmd OUTPUT_DIRECTORY [Client^|Server^|All]
  exit /b 2
)
set "ROLE=%~2"
if "%ROLE%"=="" set "ROLE=Client"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Export-RotoWeaveArtifacts.ps1" -OutputDirectory "%~1" -Role "%ROLE%"
exit /b %ERRORLEVEL%
