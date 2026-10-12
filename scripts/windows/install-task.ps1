<#
.SYNOPSIS
    Start the job-agent daemon automatically when you log on to Windows (Task Scheduler).

.DESCRIPTION
    Registers a scheduled task "\job-agent\job-agent daemon" for the CURRENT USER that runs
        <repo>\.venv\Scripts\job-agent.exe daemon
    at logon (after a 1 minute delay), with:
      * restart on failure (3 attempts, 5 minutes apart),
      * "run as soon as possible after a scheduled start is missed" (StartWhenAvailable),
      * no run-time limit (the default 72 h limit would stop the daemon),
      * keeps running on battery,
      * never a second copy (MultipleInstances IgnoreNew; job-agent also refuses duplicates itself).
    It runs with your normal (non-administrator) rights and only while you are logged on.
    Nothing is started now unless you pass -StartNow. Remove it with uninstall-task.ps1.

    By default the daemon runs without a console window (via a hidden PowerShell host; a window
    may flash briefly at logon). Use -ShowWindow to see its console instead (closing that window
    stops the daemon).

.PARAMETER RepoDir
    The job-agent checkout. Default: two levels above this script.
.PARAMETER WithDashboard
    Also register "\job-agent\job-agent dashboard" (runs "job-agent.exe dashboard").
.PARAMETER ShowWindow
    Run in a visible console window instead of hidden.
.PARAMETER Force
    Replace existing job-agent tasks with the same name.
.PARAMETER StartNow
    Start the task(s) right after registering them.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install-task.ps1
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install-task.ps1 -WithDashboard -StartNow
#>
[CmdletBinding()]
param(
    [string]$RepoDir = (Join-Path $PSScriptRoot "..\.."),
    [switch]$WithDashboard,
    [switch]$ShowWindow,
    [switch]$Force,
    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskPath = "\job-agent\"
$RepoDir = (Resolve-Path -LiteralPath $RepoDir).Path
$Exe = Join-Path $RepoDir ".venv\Scripts\job-agent.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Not found: $Exe. Create the virtual environment and install job-agent first (docs/setup.md)."
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "config\config.yaml"))) {
    Write-Warning "config\config.yaml not found in $RepoDir; run 'job-agent init' before starting the task."
}
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function New-JobAgentAction([string]$Command) {
    if ($ShowWindow) {
        return New-ScheduledTaskAction -Execute $Exe -Argument $Command -WorkingDirectory $RepoDir
    }
    # Hidden: a PowerShell host without a window runs job-agent.exe and passes its exit code on.
    $exeQuoted = $Exe.Replace("'", "''")
    $homeQuoted = $RepoDir.Replace("'", "''")
    $inner = "`$env:JOB_AGENT_HOME='$homeQuoted'; & '$exeQuoted' $Command; exit `$LASTEXITCODE"
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$inner`""
    return New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $RepoDir
}

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Trigger.Delay = "PT1M"  # let the network (and the Ollama app) start first

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

function Register-JobAgentTask([string]$Name, [string]$Command, [string]$Description) {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing -and -not $Force) {
        throw "Task '$TaskPath$Name' already exists. Re-run with -Force to replace it, or run uninstall-task.ps1."
    }
    $task = New-ScheduledTask -Action (New-JobAgentAction $Command) -Trigger $Trigger -Settings $Settings `
        -Principal $Principal -Description $Description
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $Name -InputObject $task -Force | Out-Null
    Write-Host "Registered '$TaskPath$Name': '$Exe $Command' at logon for $UserId"
}

$names = @("job-agent daemon")
Register-JobAgentTask "job-agent daemon" "daemon" `
    "job-agent scheduler daemon (human-supervised job search assistant). See docs/scheduling.md."
if ($WithDashboard) {
    $names += "job-agent dashboard"
    Register-JobAgentTask "job-agent dashboard" "dashboard" "job-agent dashboard on 127.0.0.1. See docs/scheduling.md."
}

if ($StartNow) {
    foreach ($name in $names) {
        Start-ScheduledTask -TaskPath $TaskPath -TaskName $name
        Write-Host "Started '$TaskPath$name'"
    }
} else {
    Write-Host ""
    Write-Host "Nothing was started now; the task(s) start at your next logon. To start now:"
    foreach ($name in $names) {
        Write-Host "  Start-ScheduledTask -TaskPath '$TaskPath' -TaskName '$name'"
    }
}

Write-Host ""
Write-Host "Check it:   $Exe schedule        (daemon state, next runs)"
Write-Host "            Get-ScheduledTaskInfo -TaskPath '$TaskPath' -TaskName 'job-agent daemon'"
Write-Host "Log file:   $(Join-Path $RepoDir 'logs\job_agent.log')"
Write-Host "Pause work: $Exe pause      (the daemon keeps running but skips scheduled tasks)"
Write-Host "Note: ending the task (Task Scheduler 'End', logoff, shutdown) kills the process; a task that"
Write-Host "      was mid-run is recorded as 'interrupted' at the next start and is not retried automatically."
