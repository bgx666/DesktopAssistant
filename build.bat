@echo off
rem 一键发版（双击运行，等价于 powershell -File build.ps1）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
pause
