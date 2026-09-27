@echo off
rem Double-click to install ISHA. Runs ISHA_Setup.ps1 for this process only (no system policy change).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ISHA_Setup.ps1" %*
if errorlevel 1 pause
