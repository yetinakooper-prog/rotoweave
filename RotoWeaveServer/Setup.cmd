@echo off
setlocal
cd /d "%~dp0.."
call "%~dp0..\Setup-RotoWeave.cmd" Server %*
exit /b %ERRORLEVEL%
