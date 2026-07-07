@echo off
rem neoxider.cmd - lets plain cmd.exe (and PowerShell) resolve the bare word
rem "neoxider" via PATHEXT, then hands off to the real bash launcher script.
rem
rem A bare `bash` on a fresh cmd.exe can resolve to the WSL stub at
rem C:\Windows\System32\bash.exe, which cannot run this MSYS script (it opens the
rem wrong filesystem / Store prompt and the launcher silently fails). Locate Git
rem Bash explicitly from its common install paths, falling back to bare bash.
setlocal
set "GITBASH=%ProgramFiles%\Git\usr\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=%ProgramFiles(x86)%\Git\usr\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=%LocalAppData%\Programs\Git\usr\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=bash"
"%GITBASH%" "%~dp0neoxider" %*
