@echo off
rem One-click release build (same as: powershell -File build.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
pause
