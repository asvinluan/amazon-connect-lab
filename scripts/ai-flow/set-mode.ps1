#Requires -Version 5.1
<#
.SYNOPSIS
  Show or flip the AI switch. Takes up to 15 seconds to reach a running Lambda.

.EXAMPLE
  .\scripts\ai-flow\set-mode.ps1               # show the current mode
  .\scripts\ai-flow\set-mode.ps1 -Mode shadow  # the AI runs, callers never hear it
  .\scripts\ai-flow\set-mode.ps1 -Mode live    # callers hear the answer
  .\scripts\ai-flow\set-mode.ps1 -Mode off     # the kill switch
#>
param(
    [ValidateSet('off', 'shadow', 'live', 'test_fail', 'test_crash', 'test_slow')]
    [string]$Mode
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = 0
. (Join-Path $PSScriptRoot '_common.ps1')

Test-Tools @('aws')
$alias = Get-InstanceAlias

if (-not $Mode) {
    $now = Get-AiMode $alias
    if (-not $now) { Stop-Here "cannot read /$alias/ai/mode. Has apply.ps1 been run?" }
    Write-Host "Current AI mode: $now"
    exit 0
}

Set-AiMode $alias $Mode
Write-Host "AI mode is now: $Mode" -ForegroundColor Green
Write-Host 'A running Lambda notices within 15 seconds.'
if ($Mode -like 'test_*') {
    Write-Host 'This mode BREAKS the AI on purpose. Set it back to off when you are done.' -ForegroundColor Yellow
}
