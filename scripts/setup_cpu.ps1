param(
    [switch]$Recreate,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$UvCache = Join-Path $ProjectRoot ".cache\uv"

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    throw "uv is required. Install uv and run this script again."
}

if ($Recreate -and (Test-Path -LiteralPath $VenvPath)) {
    Remove-Item -LiteralPath $VenvPath -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $UvCache | Out-Null
$env:UV_CACHE_DIR = $UvCache

if (-not (Test-Path -LiteralPath $PythonPath)) {
    & $uvCommand.Source venv --python 3.12 $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the CPU virtual environment failed with exit code $LASTEXITCODE."
    }
}

Push-Location $ProjectRoot
try {
    & $uvCommand.Source sync
    if ($LASTEXITCODE -ne 0) {
        throw "Installing CPU dependencies failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

if ($Verify) {
    & $PythonPath scripts\run_ocr.py --info --device cpu
    if ($LASTEXITCODE -ne 0) {
        throw "CPU runtime verification failed with exit code $LASTEXITCODE."
    }
}

Write-Host "CPU environment is ready: $VenvPath"
