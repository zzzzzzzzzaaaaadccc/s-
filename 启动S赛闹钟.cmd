@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 worlds_alarm.py
if errorlevel 1 pause
exit /b
:use_python
where python >nul 2>nul
if errorlevel 1 goto no_python
python worlds_alarm.py
if errorlevel 1 pause
exit /b
:no_python
echo Python 3.10+ is required. Install Python from https://www.python.org/downloads/
pause
