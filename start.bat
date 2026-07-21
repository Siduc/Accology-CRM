@echo off
cd /d "%~dp0"
echo Starting Accountant CRM...
echo Open http://127.0.0.1:8000 in your browser
echo Keep this window open while using the app.
echo.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
