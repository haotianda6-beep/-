param(
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8011
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (!(Test-Path ".venv")) {
  py -3.11 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (!(Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example. Keep LIVE_TRADING=false until demo is verified."
}

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host $HostName --port $Port

