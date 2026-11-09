#Requires -Version 5.1
<#
.SYNOPSIS
  Show what the AI Lambda did recently: time, mode, status, reason, speed, contact id.

.DESCRIPTION
  Reads the Lambda's CloudWatch log lines. The question is never in them.
  Times are shown in YOUR local time (the AWS console shows UTC).

.EXAMPLE
  .\scripts\ai-flow\audit-trail.ps1               # last 30 minutes
  .\scripts\ai-flow\audit-trail.ps1 -Minutes 120
#>
param([int]$Minutes = 30)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = 0
. (Join-Path $PSScriptRoot '_common.ps1')

Test-Tools @('aws')
$alias = Get-InstanceAlias
$group = "/aws/lambda/$alias-ai-assist"
$start = [DateTimeOffset]::UtcNow.AddMinutes(-$Minutes).ToUnixTimeMilliseconds()

$raw = & aws logs filter-log-events --log-group-name $group --start-time $start --filter-pattern ai_assist --output json 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { Stop-Here "could not read $group.`n$raw" }
$events = @((($raw | ConvertFrom-Json).events))

$rows = @()
foreach ($e in $events) {
    try { $line = $e.message | ConvertFrom-Json } catch { continue }
    if ((Get-Prop $line 'event') -ne 'ai_assist') { continue }
    $rows += [pscustomobject]@{
        Time      = [DateTimeOffset]::FromUnixTimeMilliseconds($e.timestamp).LocalDateTime.ToString('HH:mm:ss')
        Mode      = Get-Prop $line 'mode'
        Status    = Get-Prop $line 'status'
        Reason    = Get-Prop $line 'reason'
        Ms        = Get-Prop $line 'latency_ms'
        ContactId = Get-Prop $line 'contact_id'
    }
}

if ($rows.Count -eq 0) {
    Write-Host "No AI calls in the last $Minutes minutes."
    exit 0
}
$rows | Sort-Object Time | Format-Table -AutoSize | Out-Host
Write-Host ("{0} AI calls in the last {1} minutes." -f $rows.Count, $Minutes)
