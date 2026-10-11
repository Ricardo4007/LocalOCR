param(
    [switch]$Recreate,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPath = Join-Path $ProjectRoot ".venv-train"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$RequirementsPath = Join-Path $ProjectRoot "requirements-gpu.txt"
$UvCache = Join-Path $ProjectRoot ".cache\uv"
$ModelCache = Join-Path $ProjectRoot "models\paddlex"

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    throw "uv is required. Install uv and run this script again."
}
if (-not (Test-Path -LiteralPath $RequirementsPath)) {
    throw "GPU requirements file is missing: $RequirementsPath"
}

if ($Recreate -and (Test-Path -LiteralPath $VenvPath)) {
    Remove-Item -LiteralPath $VenvPath -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $UvCache, $ModelCache | Out-Null
$env:UV_CACHE_DIR = $UvCache
$env:PADDLE_PDX_CACHE_HOME = $ModelCache
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    & $uvCommand.Source venv --python 3.12 $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the GPU virtual environment failed with exit code $LASTEXITCODE."
    }
}

& $uvCommand.Source pip install --index-strategy unsafe-best-match --python $PythonPath --requirement $RequirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "Installing GPU dependencies failed with exit code $LASTEXITCODE."
}

if ($Verify) {
    $verifyCode = @"
import paddle
import paddleocr

cuda = bool(paddle.device.is_compiled_with_cuda())
count = int(paddle.device.cuda.device_count()) if cuda else 0
print(f"PaddlePaddle: {paddle.__version__}")
print(f"PaddleOCR: {paddleocr.__version__}")
print(f"CUDA compiled: {cuda}")
print(f"GPU count: {count}")
if not cuda or count < 1:
    raise SystemExit("No usable CUDA device was detected.")
paddle.set_device("gpu:0")
print("Selected device: gpu:0")
"@
    & $PythonPath -c $verifyCode
    if ($LASTEXITCODE -ne 0) {
        throw "GPU runtime verification failed with exit code $LASTEXITCODE."
    }
}

Write-Host "GPU environment is ready: $VenvPath"
