param(
    [Alias("Input")]
    [string]$InputPath = "input",
    [Alias("Output")]
    [string]$OutputPath = "output",
    [string]$Device = "auto",
    [switch]$Gpu,
    [switch]$DocOrientation,
    [switch]$Unwarp,
    [switch]$LineOrientation,
    [switch]$Info
)

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = $null

if ($Gpu) {
    $Device = "gpu:0"
}

if ($Device -like "gpu*") {
    $PythonPath = Join-Path $ProjectRoot ".venv-train\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw "GPU environment is missing. Run .\scripts\setup_train.ps1 -Verify first."
    }
    $arguments = @($PythonPath, "scripts/run_ocr.py", "--input", $InputPath, "--output", $OutputPath, "--device", $Device)
} else {
    $arguments = @("run", "python", "scripts/run_ocr.py", "--input", $InputPath, "--output", $OutputPath, "--device", $Device)
}

$env:PADDLE_PDX_CACHE_HOME = Join-Path $ProjectRoot "models\paddlex"
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"
if ($DocOrientation) { $arguments += "--doc-orientation" }
if ($Unwarp) { $arguments += "--unwarp" }
if ($LineOrientation) { $arguments += "--line-orientation" }
if ($Info) { $arguments += "--info" }

if ($Device -like "gpu*") {
    & $arguments[0] $arguments[1..($arguments.Count - 1)]
} else {
    & uv @arguments
}
