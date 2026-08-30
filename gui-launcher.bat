@echo off
rem Double-click entry point for the neoxider web GUI. The real chain
rem (neoxider.cmd -> git bash -> pythonw gui.py) runs in its own hidden console so no
rem command-line window stays open on screen. Set NEOXIDER_GUI_VISIBLE=1 to keep this console instead
rem (the launcher tests and debugging do that).
if "%NEOXIDER_GUI_VISIBLE%"=="1" goto :direct
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "& '%~dp0bin\neoxider.cmd' gui 8765" >nul 2>&1
exit /b 0
:direct
call "%~dp0bin\neoxider.cmd" gui 8765
exit /b %ERRORLEVEL%
