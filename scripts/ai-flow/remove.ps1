#Requires -Version 5.1
<#
.SYNOPSIS
  Clean up after the phone tests: switch the AI off, release claimed phone numbers,
  and remove the AI flow. The guardrail, the Lambda and the rest of the lab stay.

.DESCRIPTION
  The phone number is the only part of this sitting that costs money while idle,
  so it goes first. You are asked before each number is released.

.EXAMPLE
  .\scripts\ai-flow\remove.ps1
#>
Set-StrictMode -Version 2
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = 0
. (Join-Path $PSScriptRoot '_common.ps1')

Test-Tools @('aws', 'terraform')
$alias = Get-InstanceAlias
Confirm-Account

# ---- 1. the kill switch --------------------------------------------------------
if (Get-AiMode $alias) {
    Set-AiMode $alias 'off'
    Write-Host 'AI mode set to off.' -ForegroundColor Green
}

# ---- 2. phone numbers ----------------------------------------------------------
$arn = & aws connect list-instances --query "InstanceSummaryList[?InstanceAlias=='$alias'].Arn | [0]" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $arn -or $arn -eq 'None') { Stop-Here "cannot find the Connect instance '$alias'." }
$numbers = & aws connect list-phone-numbers-v2 --target-arn $arn --output json 2>$null | ConvertFrom-Json
$list = @($numbers.ListPhoneNumbersSummaryList)
if ($list.Count -eq 0) {
    Write-Host 'No phone numbers are claimed on this instance.'
} else {
    foreach ($n in $list) {
        $id = Get-Prop $n 'PhoneNumberId'
        Write-Host ("Claimed number: {0}  ({1})" -f (Get-Prop $n 'PhoneNumber'), (Get-Prop $n 'PhoneNumberType'))
        if ((Read-Host 'Type yes to RELEASE this number') -eq 'yes') {
            & aws connect release-phone-number --phone-number-id $id 2>&1 | Out-Host
            if ($LASTEXITCODE -eq 0) { Write-Host 'Released.' -ForegroundColor Green }
            else { Write-Host 'Release failed. Paste the message to Claude.' -ForegroundColor Red }
        } else {
            Write-Host 'Kept. It keeps costing a small amount every day.' -ForegroundColor Yellow
        }
    }
}

# ---- 3. the AI flow ------------------------------------------------------------
Write-Host ''
if ((Read-Host 'Type yes to remove lab-ai-flow and its Lambda permission') -ne 'yes') {
    Write-Host 'The flow was kept. Nothing else was changed.'
    exit 0
}
Push-Location $AiTerraform
try {
    & terraform destroy -input=false -auto-approve '-target=aws_connect_contact_flow.ai' '-target=aws_connect_lambda_function_association.ai_assist' | Out-Host
    if ($LASTEXITCODE -ne 0) { Stop-Here 'terraform destroy failed. If it says the flow is in use, release the phone number first. Paste the message to Claude.' }
}
finally { Pop-Location }
Write-Host ''
Write-Host 'Clean-up done. Kept on purpose: the guardrail, the Lambda, the mode switch (set to off).' -ForegroundColor Green
