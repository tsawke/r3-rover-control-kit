@echo off
setlocal
where py.exe >nul 2>&1
if errorlevel 1 goto use_python
py -3 -B "%~dp0control_kit.py" %*
exit /b %errorlevel%
:use_python
where python.exe >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.10 or newer and add it to PATH.
    exit /b 1
)
python -B "%~dp0control_kit.py" %*
exit /b %errorlevel%
