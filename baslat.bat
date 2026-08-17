@echo off
cd /d "%~dp0"
where python >nul 2>&1
if %errorlevel%==0 (
  python run.py
) else (
  py -3 run.py
)
pause
