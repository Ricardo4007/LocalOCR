param(
    [switch]$SkipRuntimeDownload
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildRoot = Join-Path $ProjectRoot ".build\delivery"
$Stage = Join-Path $BuildRoot "stage"
$Runtime = Join-Path $Stage "runtime"
$PythonZip = Join-Path $ProjectRoot ".cache\python-3.12.9-embed-amd64.zip"
$Archive = Join-Path $BuildRoot "LocalOCR.7z"
$GeneratedLauncherSource = Join-Path $BuildRoot "PortableLauncher.generated.cs"
$LauncherExe = Join-Path $BuildRoot "LocalOCR_Launcher.exe"
$Embedded7zExe = Join-Path $BuildRoot "7z-embedded.exe"
$Embedded7zDll = Join-Path $BuildRoot "7z-embedded.dll"
$Deliver = Join-Path $ProjectRoot "deliver"
$OutputExe = Join-Path $Deliver "LocalOCR_Portable.exe"
$SevenZipCommand = Get-Command 7z.exe -ErrorAction SilentlyContinue
$SevenZip = if ($SevenZipCommand) { $SevenZipCommand.Source } else { Join-Path $env:ProgramFiles "7-Zip\7z.exe" }
$SevenZipDll = Join-Path (Split-Path -Parent $SevenZip) "7z.dll"
$Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$LauncherSource = Join-Path $PSScriptRoot "PortableLauncher.cs"
$LauncherManifest = Join-Path $PSScriptRoot "launcher.manifest"
$PythonEmbedUrl = "https://www.python.org/ftp/python/3.12.9/python-3.12.9-embed-amd64.zip"

if (-not (Test-Path -LiteralPath $ProjectRoot)) { throw "Project root missing: $ProjectRoot" }
if (-not (Test-Path -LiteralPath $SevenZip)) { throw "7-Zip not found: $SevenZip" }
if (-not (Test-Path -LiteralPath $SevenZipDll)) { throw "7-Zip DLL not found: $SevenZipDll" }
if (-not (Test-Path -LiteralPath $Csc)) { throw "C# compiler not found: $Csc" }
if (-not (Test-Path -LiteralPath $LauncherSource)) { throw "Launcher source missing: $LauncherSource" }
if (-not (Test-Path -LiteralPath $LauncherManifest)) { throw "Launcher manifest missing: $LauncherManifest" }

New-Item -ItemType Directory -Force -Path $BuildRoot, $Deliver, (Join-Path $ProjectRoot ".cache") | Out-Null
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage, $Runtime | Out-Null
if (Test-Path -LiteralPath $Archive) { Remove-Item -LiteralPath $Archive -Force }

if (-not (Test-Path -LiteralPath $PythonZip)) {
    if ($SkipRuntimeDownload) { throw "Embedded Python archive is missing: $PythonZip" }
    Invoke-WebRequest -Uri $PythonEmbedUrl -OutFile $PythonZip
}
Expand-Archive -LiteralPath $PythonZip -DestinationPath $Runtime -Force

$pth = Join-Path $Runtime "python312._pth"
@"
python312.zip
.
Lib\site-packages
import site
"@ | Set-Content -LiteralPath $pth -Encoding ascii

$sitePackages = Join-Path $Runtime "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
$sourcePackages = Join-Path $ProjectRoot ".venv-train\Lib\site-packages"
if (-not (Test-Path -LiteralPath $sourcePackages)) { throw "Training environment is missing: $sourcePackages" }
& robocopy $sourcePackages $sitePackages /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XF *.pyc /XD __pycache__ pip wheel pyinstaller altgraph pefile pywin32_ctypes
if ($LASTEXITCODE -gt 7) { throw "Copying Python packages failed: robocopy exit code $LASTEXITCODE" }

New-Item -ItemType Directory -Force -Path (Join-Path $Stage "scripts") | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts\run_ocr.py") -Destination (Join-Path $Stage "scripts\run_ocr.py") -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts\web_console.py") -Destination (Join-Path $Stage "scripts\web_console.py") -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts\portable_start.py") -Destination (Join-Path $Stage "scripts\portable_start.py") -Force
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "web") | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "web\index.html") -Destination (Join-Path $Stage "web\index.html") -Force
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "models") | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "models\paddlex") -Destination (Join-Path $Stage "models") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "input"), (Join-Path $Stage "output"), (Join-Path $Stage "runtime-tmp") | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "packaging\manual.html") -Destination (Join-Path $BuildRoot "manual.html") -Force

$edgeCandidates = @(Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
if (${env:ProgramFiles(x86)}) {
    $edgeCandidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"
}
$edge = $edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge is required to create the PDF manual." }
$manualPdf = Join-Path $Stage "manual.pdf"
$manualHtml = Join-Path $BuildRoot "manual.html"
& $edge --headless --disable-gpu --run-all-compositor-stages-before-draw "--print-to-pdf=$manualPdf" ("file:///" + ($manualHtml -replace "\\", "/")) | Out-Null
if (-not (Test-Path -LiteralPath $manualPdf)) { throw "PDF manual was not created." }
Remove-Item -LiteralPath $manualHtml -Force

$launcher = @'
@echo off
setlocal
set "APP_ROOT=%~dp0"
start "Local OCR" /b "%APP_ROOT%runtime\python.exe" "%APP_ROOT%scripts\portable_start.py"
exit /b 0
'@
$launcher | Set-Content -LiteralPath (Join-Path $Stage "start_ocr.cmd") -Encoding ascii

Push-Location $Stage
try {
    & $SevenZip a -t7z -mx=1 -mmt=on $Archive * | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "7-Zip archive creation failed: $LASTEXITCODE" }
} finally { Pop-Location }

$deliveryVersion = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.Substring(0, 16)
$launcherText = Get-Content -LiteralPath $LauncherSource -Raw
$launcherText = $launcherText.Replace("__DELIVERY_VERSION__", $deliveryVersion)
[System.IO.File]::WriteAllText($GeneratedLauncherSource, $launcherText, [System.Text.UTF8Encoding]::new($false))
Copy-Item -LiteralPath $SevenZip -Destination $Embedded7zExe -Force
Copy-Item -LiteralPath $SevenZipDll -Destination $Embedded7zDll -Force
& $Csc /nologo /target:winexe /optimize+ /out:$LauncherExe /win32manifest:$LauncherManifest /reference:System.Windows.Forms.dll /resource:"$Embedded7zExe,7z.exe" /resource:"$Embedded7zDll,7z.dll" $GeneratedLauncherSource | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Portable launcher compilation failed: $LASTEXITCODE" }

function Append-File([System.IO.FileStream]$target, [string]$path) {
    $source = [System.IO.File]::OpenRead($path)
    try { $source.CopyTo($target, 1048576) } finally { $source.Dispose() }
}
$output = [System.IO.File]::Open($OutputExe, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
try {
    Append-File $output $LauncherExe
    Append-File $output $Archive
} finally { $output.Dispose() }

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $OutputExe).Hash
Write-Host "Created: $OutputExe"
Write-Host "SHA256: $hash"
