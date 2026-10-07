@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
where /q py.exe >nul 2>&1
if not errorlevel 1 goto :py
:find_python
where /q python.exe >nul 2>&1
if not errorlevel 1 goto :python
where /q python3.exe >nul 2>&1
if not errorlevel 1 goto :python3
echo neoxider: Python 3.8+ is required. Install Python from python.org and enable "Add Python to PATH", then open a new shell. 1>&2
exit /b 127
:py
set "NEOXIDER_PYTHON="
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "NEOXIDER_PYTHON=%%P"
if not defined NEOXIDER_PYTHON goto :find_python
"%NEOXIDER_PYTHON%" "%~dp0..\agent.py" %*
exit /b %ERRORLEVEL%
:python
python "%~dp0..\agent.py" %*
exit /b %ERRORLEVEL%
:python3
python3 "%~dp0..\agent.py" %*
exit /b %ERRORLEVEL%
