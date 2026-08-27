@echo off
cd /d C:\Users\User\accountant-crm
set PYTHONPATH=C:\Users\User\accountant-crm
set DATABASE_URL=
if not exist logs mkdir logs
echo ==== start %date% %time% ====>> logs\uvicorn-local.log
start "Accologise CRM" "%LocalAppData%\Programs\Python\Python314\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
