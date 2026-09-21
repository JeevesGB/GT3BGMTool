@echo off
rem Start GT3BGMTool. Double-click this file - no command line needed.
setlocal
cd /d "%~dp0"

rem pythonw runs it without a console window hanging around behind the tool
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "gt3bgmtool.py"
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "gt3bgmtool.py"
    exit /b
)

echo.
echo Python 3 was not found on your PATH.
echo.
echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH"
echo during setup - then double-click this file again.
echo.
pause
