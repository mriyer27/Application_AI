<#
.SYNOPSIS
    Remove the job-agent scheduled tasks created by install-task.ps1.

.DESCRIPTION
    Stops (unless -NoStop) and unregisters "\job-agent\job-agent daemon",
    "\job-agent\job-agent dashboard" and the optional "\job-agent\job-agent backup" task for the
    current user, then removes the empty "\job-agent\" folder. Your data, config and logs are
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
foreach ($name in @("job-agent daemon", "job-agent dashboard", "job-agent backup")) {
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

# Remove the now-empty \job-agent\ task folder (ignored if other tasks remain or it does not exist).
try {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\job-agent")
    if ($folder.GetTasks(1).Count -eq 0) {
        $service.GetFolder("\").DeleteFolder("job-agent", 0)
        Write-Host "Removed task folder \job-agent\"
    }
} catch {
    # folder absent or not empty: nothing to do
}
