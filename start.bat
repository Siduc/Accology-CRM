@echo off
cd /d "%~dp0"
echo Starting Accologise CRM...
echo Open http://127.0.0.1:8000 in your browser
echo Keep this window open while using the app.
echo.
echo Database: local SQLite crm.db (fast on this laptop).
echo           Comment out DATABASE_URL in .env. Leave RENDER_DATABASE_URL
echo           for the 17:00 phone/demo publish. If DATABASE_URL is still
echo           set to Render Postgres, every page will wait on Ohio.
echo.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 "%LocalAppData%\Programs\Python\Python314\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
