# Shared helpers for the ai-flow scripts. Dot-sourced by the other scripts; do not run directly.
Set-StrictMode -Version 2

$AiRepo      = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$AiTerraform = Join-Path $AiRepo 'terraform'

if (-not $env:AWS_REGION) { $env:AWS_REGION = 'ap-southeast-2' }
$env:AWS_DEFAULT_REGION = $env:AWS_REGION

# The only resources this sitting creates or changes. Everything else in the
# terraform folder (including an old ServiceNow experiment) is left alone.
$AiTargets = @(
    '-target=aws_ssm_parameter.ai_mode',
    '-target=aws_iam_role_policy.ai_assist_mode',
    '-target=aws_lambda_function.ai_assist',
    '-target=aws_connect_lambda_function_association.ai_assist',
    '-target=aws_connect_contact_flow.ai'
)

function Stop-Here([string]$Message) {
    Write-Host "STOPPED: $Message" -ForegroundColor Red
    exit 1
}

function Test-Tools([string[]]$Names) {
    foreach ($tool in $Names) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            $fix = 'winget install Amazon.AWSCLI'
            if ($tool -eq 'terraform') { $fix = 'winget install Hashicorp.Terraform' }
            Stop-Here "'$tool' is not installed or PowerShell cannot see it. Run:  $fix   then close and reopen PowerShell."
        }
    }
}

function Get-InstanceAlias {
    $tfvars = Join-Path $AiTerraform 'terraform.tfvars'
    if (-not (Test-Path $tfvars)) { Stop-Here 'terraform\terraform.tfvars is missing.' }
    $line = Select-String -Path $tfvars -Pattern '^\s*instance_alias\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $line) { Stop-Here 'instance_alias is not set in terraform\terraform.tfvars.' }
    return $line.Matches[0].Groups[1].Value
}

function Confirm-Account {
    $who = & aws sts get-caller-identity --output json 2>$null
    if ($LASTEXITCODE -ne 0) { Stop-Here 'no working AWS login. Run  aws configure  (or  aws sso login ) and try again.' }
    $id = $who | ConvertFrom-Json
    Write-Host "AWS account : $($id.Account)"
    Write-Host "Signed in as: $($id.Arn)"
    Write-Host 'This must be YOUR personal lab account. Never an employer or customer account.' -ForegroundColor Yellow
    if ((Read-Host 'Type yes to continue') -ne 'yes') { Write-Host 'Cancelled. Nothing was changed.'; exit 1 }
}

function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

# Windows PowerShell 5.1 writes a BOM with -Encoding UTF8, which breaks JSON files for the AWS CLI.
function Write-JsonFile([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Set-AiMode([string]$Alias, [string]$Mode) {
    & aws ssm put-parameter --name "/$Alias/ai/mode" --value $Mode --type String --overwrite --output text 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Stop-Here "could not set /$Alias/ai/mode. Has the apply been run? (plan.ps1, then apply.ps1)"
    }
}

function Get-AiMode([string]$Alias) {
    $v = & aws ssm get-parameter --name "/$Alias/ai/mode" --query Parameter.Value --output text 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($v | Out-String).Trim()
}
