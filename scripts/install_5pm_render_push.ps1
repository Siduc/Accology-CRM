# Register (disabled by default) the 17:00 Render publish + missed-run-on-logon.
#   powershell -ExecutionPolicy Bypass -File scripts\install_5pm_render_push.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_5pm_render_push.ps1 -Enable
#   powershell -ExecutionPolicy Bypass -File scripts\install_5pm_render_push.ps1 -Remove
param(
    [switch]$Enable,
    [switch]$Remove
)

$TaskName = "AccologyCRM-PublishToRender"
$Root = "C:\Users\User\accountant-crm"
$Script = Join-Path $Root "scripts\scheduled_push_to_render.ps1"
$Pwsh = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if ($Remove) {
    schtasks /Delete /TN $TaskName /F 2>$null
    Write-Output "Removed $TaskName"
    exit 0
}

if (-not (Test-Path -LiteralPath $Script)) {
    Write-Output "Missing $Script"
    exit 1
}

# Daily 17:00, run missed start when the laptop next wakes.
$create = @(
    "/Create", "/F",
    "/TN", $TaskName,
    "/SC", "DAILY",
    "/ST", "17:00",
    "/RL", "LIMITED",
    "/TR", "`"$Pwsh`" -NoProfile -ExecutionPolicy Bypass -File `"$Script`" scheduled"
)
$out = & schtasks @create 2>&1
Write-Output $out

# Second trigger: at logon (catch-up if 17:00 was missed). Same task cannot easily
# have two triggers via schtasks /Create, so register a sibling.
$LogonName = "AccologyCRM-PublishToRender-Logon"
schtasks /Delete /TN $LogonName /F 2>$null | Out-Null
$create2 = @(
    "/Create", "/F",
    "/TN", $LogonName,
    "/SC", "ONLOGON",
    "/RL", "LIMITED",
    "/DELAY", "0002:00",
    "/TR", "`"$Pwsh`" -NoProfile -ExecutionPolicy Bypass -File `"$Script`" logon"
)
$out2 = & schtasks @create2 2>&1
Write-Output $out2

if (-not $Enable) {
    schtasks /Change /TN $TaskName /DISABLE | Out-Null
    schtasks /Change /TN $LogonName /DISABLE | Out-Null
    Write-Output "Registered DISABLED. Enable after you switch this laptop to local crm.db:"
    Write-Output "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Enable"
} else {
    schtasks /Change /TN $TaskName /ENABLE | Out-Null
    schtasks /Change /TN $LogonName /ENABLE | Out-Null
    Write-Output "Enabled $TaskName (17:00) and $LogonName (catch-up)."
}

schtasks /Query /TN $TaskName /FO LIST /V | Select-String -Pattern "Task Name|Status|Start Time|Task To Run"
schtasks /Query /TN $LogonName /FO LIST | Select-String -Pattern "Task Name|Status"
