@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo Document Intelligence Console Enterprise v1.08 Source Release / EXE Packaging Unsupported
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
echo Build completed. v1.08 dual-process EXE packaging is not implemented.
pause
