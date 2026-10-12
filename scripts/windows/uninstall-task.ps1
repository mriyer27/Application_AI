<#
.SYNOPSIS
    Remove the job-agent scheduled tasks created by install-task.ps1.

.DESCRIPTION
    Stops (unless -NoStop) and unregisters "\job-agent\job-agent daemon" and
    "\job-agent\job-agent dashboard" for the current user. Your data, config and logs are
    not touched. Stopping ends the process immediately: if a task was mid-run it is recorded
    as "interrupted" the next time the daemon starts (run `job-agent pause` first and wait for
    `job-agent schedule` to show no lock held if you want to avoid that).

.PARAMETER NoStop
    Unregister only; leave an already running daemon/dashboard process alone.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\uninstall-task.ps1
#>
[CmdletBinding()]
param([switch]$NoStop)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskPath = "\job-agent\"
foreach ($name in @("job-agent daemon", "job-agent dashboard")) {
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Not registered: $TaskPath$name"
        continue
    }
    if (-not $NoStop -and $task.State -eq "Running") {
        Stop-ScheduledTask -TaskPath $TaskPath -TaskName $name
        Write-Host "Stopped $TaskPath$name"
    }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false
    Write-Host "Removed $TaskPath$name"
}
