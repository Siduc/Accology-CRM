# Start the local Accologise CRM on 127.0.0.1:8000 if it is not already listening.
# Safe to run from Task Scheduler (outside any chat/job wrapper).
$Root = "C:\Users\User\accountant-crm"
$LogDir = Join-Path $Root "logs"
$OutLog = Join-Path $LogDir "uvicorn-local.log"
$ErrLog = Join-Path $LogDir "uvicorn-local.err.log"
$Py = "$env:LocalAppData\Programs\Python\Python314\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    $Py = "python"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$listen = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Output "already listening pid=$($listen.OwningProcess | Select-Object -Unique)"
    exit 0
}

$env:PYTHONPATH = $Root
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $OutLog -Value "`n==== start $stamp ===="
Add-Content -Path $ErrLog -Value "`n==== start $stamp ===="

Start-Process -FilePath $Py -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -ArgumentList @(
        "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1",
        "--port", "8000"
    )

$ok = $false
foreach ($i in 1..25) {
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
        $ok = $true
        break
    }
}
if ($ok) {
    Write-Output "started http://127.0.0.1:8000"
    exit 0
}
Write-Output "FAILED to bind 8000 — see $ErrLog"
Get-Content $ErrLog -Tail 40
exit 1
