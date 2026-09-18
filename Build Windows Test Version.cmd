@echo off
setlocal
cd /d "%~dp0"
set LOG=%USERPROFILE%\Desktop\WoW-Log-Data-Analyst-build.log
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" > "%LOG%" 2>&1
if errorlevel 1 (
  echo Build failed. Log: %LOG%
  start "" notepad "%LOG%"
  exit /b 1
)
echo Build completed.
for /f "usebackq delims=" %%V in ("VERSION") do set VERSION=%%V
if exist "WoW-Log-Data-Analyst-v%VERSION%-Windows-Portable.zip" (
  start "" explorer /select,"%CD%\WoW-Log-Data-Analyst-v%VERSION%-Windows-Portable.zip"
)
endlocal
