@echo off
setlocal
cd /d "%~dp0"
if not exist "web\index.html" goto incomplete
if defined MEKO_API_KEY goto full
if defined DATABASE_URL goto full
py -3 -c "import sys;sys.exit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto python
py -3 local_server.py
goto finished
:python
python -c "import sys;sys.exit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto missing
python local_server.py
goto finished
:full
echo Starting the FastAPI backend. When startup finishes, open http://127.0.0.1:8000/
call start-full.cmd
:finished
if not errorlevel 1 exit /b 0
echo Startup stopped. The error appears above.
pause
exit /b 1
:missing
echo Install Python 3.11 or newer for Windows, then run start.cmd again.
pause
exit /b 1
:incomplete
echo Extract the entire Scarline ZIP before running start.cmd.
pause
exit /b 1
