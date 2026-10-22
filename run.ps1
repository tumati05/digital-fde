# Windows PowerShell: .\run.ps1
# First run: creates .venv, installs requirements, copies .env.example to .env (then stops so you can fill it in).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host 'Created .env. Set SUPERVITY_TOKEN and WORKFLOW_ID in it, then run .\run.ps1 again.'
  exit 0
}
Get-Content .env | Where-Object { $_ -match '^\s*[^#\s].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
}

$hostBind = '127.0.0.1'
$workers = '1'
$extra = @()
$appEnv = if ($env:APP_ENV) { $env:APP_ENV.ToLower() } else { 'development' }
if ($appEnv -eq 'production') {
  $hostBind = '0.0.0.0'
  $workers = '1'
}
$trust = if ($env:TRUST_PROXY) { $env:TRUST_PROXY.ToLower() } else { '' }
if ($trust -in @('1', 'true', 'yes')) {
  $extra += '--proxy-headers'
}
& .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host $hostBind --port 8000 --workers $workers @extra
