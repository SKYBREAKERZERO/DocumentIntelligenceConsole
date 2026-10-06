@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo Document Intelligence Console Enterprise v1.03 EXE Builder
echo Windows x64 / PyInstaller OneFile
echo ============================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1" -Bootstrap -Clean
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Review the error above.
  pause
  exit /b 1
)
echo.
echo Build completed. Open: release\v1.03
pause
