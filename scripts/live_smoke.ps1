# Windows PowerShell wrapper for the live smoke test.
# Run from the connectwise-mcp folder:  .\scripts\live_smoke.ps1
# Prompts for any CW_* value not already set in the environment.

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -q -e ".[dev]"
}

function Get-CwValue([string]$Name, [string]$Prompt) {
    $current = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($current)) {
        $value = Read-Host $Prompt
        Set-Item -Path "Env:$Name" -Value $value
    }
}

Get-CwValue "CW_COMPANY_ID"  "ConnectWise company ID (login company name)"
Get-CwValue "CW_PUBLIC_KEY"  "Public key"
Get-CwValue "CW_PRIVATE_KEY" "Private key"
Get-CwValue "CW_CLIENT_ID"   "Client ID (GUID)"
if ([string]::IsNullOrWhiteSpace($env:CW_REGION)) { $env:CW_REGION = "na" }

& .\.venv\Scripts\python.exe scripts\live_smoke.py
