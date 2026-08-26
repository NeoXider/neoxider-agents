@echo off
rem Repository-local GUI launcher. The shared Windows entrypoint locates Git Bash,
rem preserves the real exit code, and invokes this checkout rather than a stale installed copy.
setlocal
title Neoxider Agents GUI
call "%~dp0bin\neoxider.cmd" gui 8765
exit /b %ERRORLEVEL%
