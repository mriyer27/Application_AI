<#
.SYNOPSIS
    Consistent online backup of the job-agent SQLite database (Windows).

.DESCRIPTION
    Thin wrapper around "job-agent db backup" (safe while the daemon/dashboard run).
    Without -Dest the backup goes to data\backups\job_agent_<UTC timestamp>.db.
    Old backups are never deleted by this script. Backups contain your application
    history: keep them private.

    Daily at 22:05 with Task Scheduler (current user, runs only when logged on):
      $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"C:\path\to\Application_AI\scripts\backup-db.ps1`""
      Register-ScheduledTask -TaskPath "\job-agent\" -TaskName "job-agent backup" -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At 22:05) -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
    (uninstall-task.ps1 removes it too.)

.PARAMETER RepoDir
    The job-agent checkout. Default: the parent of this script's directory.
.PARAMETER Dest
    Optional destination file.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup-db.ps1
#>
[CmdletBinding()]
param(
    [string]$RepoDir = (Join-Path $PSScriptRoot ".."),
    [string]$Dest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoDir = (Resolve-Path -LiteralPath $RepoDir).Path
$Exe = Join-Path $RepoDir ".venv\Scripts\job-agent.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    [Console]::Error.WriteLine("Not found: $Exe. Create the virtual environment first (docs/setup.md).")
    exit 2
}

$env:JOB_AGENT_HOME = $RepoDir
$code = 1  # reported if job-agent could not be started at all
Push-Location -LiteralPath $RepoDir
try {
    if ($Dest) {
        & $Exe db backup --dest $Dest
    } else {
        & $Exe db backup
    }
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
