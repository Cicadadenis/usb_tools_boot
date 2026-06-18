@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Cicada USB Boot Tool - PyInstaller build
echo ============================================
echo.

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo ERROR: pyinstaller not found. Install: pip install pyinstaller
    exit /b 1
)

echo [1/3] Removing old build and dist...
if exist build (
    rmdir /s /q build
)
if exist dist (
    rmdir /s /q dist
)

echo [2/3] Running PyInstaller (onefile)...
rem onefile, windowed, icon and name are defined in CicadaUSBBoot.spec
rem (--onefile/--windowed/--icon/--name are not accepted together with a .spec file)
pyinstaller --clean --noconfirm CicadaUSBBoot.spec
if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    exit /b 1
)

echo.
echo [3/3] Build complete.
echo.
if exist "dist\CicadaUSBBoot.exe" (
    for %%A in ("dist\CicadaUSBBoot.exe") do echo Ready EXE: %%~fA  ^(%%~zA bytes^)
) else (
    echo ERROR: dist\CicadaUSBBoot.exe not found.
    exit /b 1
)
echo.
echo Single-file build: copy only dist\CicadaUSBBoot.exe to the target PC.
echo Place Cicada3301.7z, FAT32.7z and 7z.exe next to the EXE if needed.
echo.
endlocal
