# Daily publish: local crm.db → Render Postgres.
# Safe no-op while this laptop still uses DATABASE_URL (shared Ohio/EU book).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $Root "crm.db"))) {
    $Root = "C:\Users\User\accountant-crm"
}
$EnvFile = Join-Path $Root ".env"
$Py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("render-push-{0:yyyyMMdd}.log" -f (Get-Date))
$Stamp = Join-Path $LogDir "last-render-push.txt"

function Write-Log($m) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m
    Add-Content -LiteralPath $Log -Value $line
    Write-Output $line
}

function Get-DotEnv([string]$Key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $null }
    foreach ($raw in Get-Content -LiteralPath $EnvFile) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) { continue }
        if ($line.Substring(0, $eq).Trim() -eq $Key) {
            return $line.Substring($eq + 1).Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

$reason = $args[0]
Write-Log "start reason=$reason"

$dbUrl = Get-DotEnv "DATABASE_URL"
if ($dbUrl -and $dbUrl -notmatch "^\s*sqlite") {
    Write-Log "SKIP laptop still using Render as the live book (DATABASE_URL is set). Push would overwrite it with old crm.db."
    exit 0
}

$renderUrl = Get-DotEnv "RENDER_DATABASE_URL"
if (-not $renderUrl) {
    Write-Log "FAIL no RENDER_DATABASE_URL in .env"
    exit 1
}

if ($reason -eq "logon") {
    if (Test-Path -LiteralPath $Stamp) {
        try {
            $last = [datetime]::Parse((Get-Content -LiteralPath $Stamp -Raw).Trim())
            if ($last.Date -eq (Get-Date).Date) {
                Write-Log "SKIP already published today at $last"
                exit 0
            }
        } catch {}
    }
}

$env:CONFIRM_PUSH = "YES"
$env:RENDER_DATABASE_URL = $renderUrl
$env:PYTHONPATH = $Root
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue

Set-Location $Root
Write-Log "running push_local_book_to_render.py"
& $Py (Join-Path $Root "scripts\push_local_book_to_render.py") *>> $Log
$code = $LASTEXITCODE
if ($code -eq 0) {
    Set-Content -LiteralPath $Stamp -Value ((Get-Date).ToString("o"))
    Write-Log "ok"
} else {
    Write-Log "FAIL exit=$code"
}
exit $code
