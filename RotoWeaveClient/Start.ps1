$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$contractsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveContracts"))
$modelsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveModels"))
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$vite = Join-Path $projectRoot "node_modules\.bin\vite.cmd"
$frontendIndex = Join-Path $projectRoot "runtime\frontend\index.html"
$stopScript = Join-Path $projectRoot "scripts\stop-rotoweave-client.ps1"

if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $vite -PathType Leaf)) {
    throw "RotoWeaveClient environment is missing. Run $projectRoot\Setup.cmd, or use the root Setup-RotoWeave.cmd Client flow from a fresh source checkout."
}
if (-not (Test-Path -LiteralPath (Join-Path $contractsRoot "product.json") -PathType Leaf)) {
    throw "RotoWeaveContracts is missing: $contractsRoot"
}
if (-not (Test-Path -LiteralPath $stopScript -PathType Leaf)) {
    throw "RotoWeaveClient stop script is missing: $stopScript"
}
$pythonPathEntries = @($contractsRoot)
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $pythonPathEntries += $env:PYTHONPATH
}
$env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator

$inputs = @(
    Get-ChildItem -LiteralPath (Join-Path $projectRoot "app") -Recurse -File
    Get-Item -LiteralPath (Join-Path $projectRoot "package-lock.json"), (Join-Path $projectRoot "vite.config.ts")
    Get-ChildItem -LiteralPath $contractsRoot -File
    Get-ChildItem -LiteralPath (Join-Path $contractsRoot "contracts") -File
)
$latestInput = ($inputs | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
$needsBuild = -not (Test-Path -LiteralPath $frontendIndex -PathType Leaf)
if (-not $needsBuild) {
    $needsBuild = (Get-Item -LiteralPath $frontendIndex).LastWriteTimeUtc -lt $latestInput
}
if ($needsBuild) {
    Push-Location $projectRoot
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "RotoWeaveClient static build failed." }
    } finally {
        Pop-Location
    }
}

if (-not [string]::IsNullOrWhiteSpace($env:ROTOWEAVE_MODELS_ROOT) -and
    -not [string]::IsNullOrWhiteSpace($env:AIFRAME_MODELS_ROOT) -and
    $env:ROTOWEAVE_MODELS_ROOT.Trim() -ne $env:AIFRAME_MODELS_ROOT.Trim()) {
    throw "ROTOWEAVE_MODELS_ROOT conflicts with the compatibility variable AIFRAME_MODELS_ROOT."
}
if ([string]::IsNullOrWhiteSpace($env:ROTOWEAVE_MODELS_ROOT) -and -not [string]::IsNullOrWhiteSpace($env:AIFRAME_MODELS_ROOT)) {
    $env:ROTOWEAVE_MODELS_ROOT = $env:AIFRAME_MODELS_ROOT
}
if ([string]::IsNullOrWhiteSpace($env:ROTOWEAVE_MODELS_ROOT)) {
    $env:ROTOWEAVE_MODELS_ROOT = $modelsRoot
}
$env:PYTHONUTF8 = "1"

& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $stopScript -ApiPort 8766
if ($LASTEXITCODE -ne 0) {
    throw "Unable to stop the existing RotoWeave client."
}

$clientProcess = Start-Process `
    -FilePath $python `
    -ArgumentList @("-m", "backend.client_launcher") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
Start-Sleep -Milliseconds 500
$clientProcess.Refresh()
if ($clientProcess.HasExited) {
    throw "RotoWeaveClient exited during startup with code $($clientProcess.ExitCode)."
}
exit 0
