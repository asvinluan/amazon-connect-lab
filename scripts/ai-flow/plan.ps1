#Requires -Version 5.1
<#
.SYNOPSIS
  Show what Terraform WOULD change for the AI flow. Changes nothing.

.DESCRIPTION
  Runs terraform init, validate and plan, limited to the five resources this
  sitting touches. Expect: 4 to add, 1 to change, 0 to destroy.
  Then run .\scripts\ai-flow\apply.ps1.

.EXAMPLE
  .\scripts\ai-flow\plan.ps1
#>
Set-StrictMode -Version 2
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = 0
. (Join-Path $PSScriptRoot '_common.ps1')

Test-Tools @('aws', 'terraform')
$alias = Get-InstanceAlias
Write-Host "Lab instance alias: $alias"
Confirm-Account

Push-Location $AiTerraform
try {
    Write-Host ''
    Write-Host 'terraform init ...'
    & terraform init -input=false | Out-Host
    if ($LASTEXITCODE -ne 0) { Stop-Here 'terraform init failed. Paste the red text to Claude.' }

    Write-Host ''
    Write-Host 'terraform validate ...'
    & terraform validate | Out-Host
    if ($LASTEXITCODE -ne 0) { Stop-Here 'terraform validate failed. Paste the red text to Claude.' }

    Write-Host ''
    Write-Host 'terraform plan (only the five AI flow resources) ...'
    $text = & terraform plan -input=false -no-color -out=tfplan-ai @AiTargets 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-Host $text
    if ($code -ne 0) { Stop-Here 'terraform plan failed. Paste the red text to Claude.' }

    $clean = [regex]::Replace($text, '\x1b\[[0-9;]*m', '')
    $m = [regex]::Match($clean, 'Plan:\s+(\d+) to add, (\d+) to change, (\d+) to destroy')
    if (-not $m.Success) { Stop-Here 'could not find the Plan line. Paste the output to Claude.' }
    $add = [int]$m.Groups[1].Value; $chg = [int]$m.Groups[2].Value; $del = [int]$m.Groups[3].Value
    Write-Host ''
    if ($add -eq 4 -and $chg -eq 1 -and $del -eq 0) {
        Write-Host 'This is the plan the lab expects: 4 to add, 1 to change, 0 to destroy.' -ForegroundColor Green
        Write-Host 'Next:  .\scripts\ai-flow\apply.ps1'
    } else {
        Write-Host "UNEXPECTED plan: $add to add, $chg to change, $del to destroy. Do NOT apply. Paste the plan to Claude." -ForegroundColor Red
        exit 1
    }
}
finally { Pop-Location }
