[CmdletBinding()]
param(
  [string]$BindHost = "",
  [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not $BindHost) {
  $BindHost = if ($env:VALIDATOR_HOST) { $env:VALIDATOR_HOST } else { "127.0.0.1" }
}
if ($Port -le 0) {
  $Port = if ($env:VALIDATOR_PORT) { [int]$env:VALIDATOR_PORT } else { 8044 }
}

if (-not (Test-Path $venvPython)) {
  $python = (Get-Command "python" -ErrorAction SilentlyContinue).Source
  if (-not $python) { $python = (Get-Command "py" -ErrorAction SilentlyContinue).Source }
  if ([IO.Path]::GetFileName($python) -ieq "py.exe") { & $python -3 -m venv (Join-Path $root ".venv") } else { & $python -m venv (Join-Path $root ".venv") }
}

$install = $env:VALIDATOR_INSTALL_DEPENDENCIES -eq "true"
if (-not $install) {
  & $venvPython -c "import fastapi, httpx, openai, pydantic_settings, uvicorn" 2>$null
  $install = $LASTEXITCODE -ne 0
}
if ($install) {
  & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "$LASTEXITCODE" }
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $root ".env.example") $envFile }

Push-Location $root
try {
  & $venvPython -m uvicorn app.main:app --host $BindHost --port $Port
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
