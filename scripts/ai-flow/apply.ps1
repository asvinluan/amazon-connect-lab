#Requires -Version 5.1
<#
.SYNOPSIS
  Apply the plan made by plan.ps1, then show the switch. The AI mode starts as off.

.EXAMPLE
  .\scripts\ai-flow\apply.ps1
#>
Set-StrictMode -Version 2
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = 0
. (Join-Path $PSScriptRoot '_common.ps1')

Test-Tools @('aws', 'terraform')
$alias = Get-InstanceAlias
if (-not (Test-Path (Join-Path $AiTerraform 'tfplan-ai'))) { Stop-Here 'no saved plan. Run .\scripts\ai-flow\plan.ps1 first.' }
Confirm-Account

Push-Location $AiTerraform
try {
    & terraform apply -input=false tfplan-ai | Out-Host
    if ($LASTEXITCODE -ne 0) { Stop-Here 'terraform apply failed. Paste the red text to Claude.' }
}
finally { Pop-Location }

Write-Host ''
$mode = Get-AiMode $alias
Write-Host "AI mode switch: /$alias/ai/mode = $mode" -ForegroundColor Green
Write-Host 'Next:  .\scripts\ai-flow\test-modes.ps1   (about 3 minutes, no phone needed)'
