@echo off
rem neoxider.cmd - lets plain cmd.exe (and PowerShell) resolve the bare word
rem "neoxider" via PATHEXT, then hands off to the real bash launcher script.
setlocal
bash "%~dp0neoxider" %*
