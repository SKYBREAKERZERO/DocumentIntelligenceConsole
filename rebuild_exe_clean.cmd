@echo off
setlocal
cd /d "%~dp0"
echo [clean] Removing old PyInstaller build outputs...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release\\v1.03 rmdir /s /q release\\v1.03
echo [build] Starting optimized v1.03 EXE build...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1" -SkipTests
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Review the error above.
  pause
  exit /b 1
)
echo.
echo BUILD SUCCESS.
pause
