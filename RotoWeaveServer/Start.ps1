$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$contractsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveContracts"))
$modelsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveModels"))
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$adminRoot = Join-Path $projectRoot "server-admin"
$vite = Join-Path $adminRoot "node_modules\.bin\vite.cmd"
$adminIndex = Join-Path $adminRoot "dist\index.html"
$stopScript = Join-Path $projectRoot "scripts\stop-rotoweave-server.ps1"

if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $vite -PathType Leaf)) {
    throw "RotoWeaveServer environment is missing. Run $projectRoot\Setup.cmd, or use the root Setup-RotoWeave.cmd Server flow from a fresh source checkout."
}
if (-not (Test-Path -LiteralPath (Join-Path $contractsRoot "product.json") -PathType Leaf)) {
    throw "RotoWeaveContracts is missing: $contractsRoot"
}
if (-not (Test-Path -LiteralPath $stopScript -PathType Leaf)) {
    throw "RotoWeaveServer stop script is missing: $stopScript"
}
$pythonPathEntries = @($contractsRoot)
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $pythonPathEntries += $env:PYTHONPATH
}
$env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator

$inputs = @(
    Get-ChildItem -LiteralPath (Join-Path $adminRoot "src") -Recurse -File
    Get-Item -LiteralPath (Join-Path $adminRoot "package-lock.json"), (Join-Path $adminRoot "vite.config.ts")
    Get-ChildItem -LiteralPath $contractsRoot -File
    Get-ChildItem -LiteralPath (Join-Path $contractsRoot "contracts") -File
)
$latestInput = ($inputs | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
$needsBuild = -not (Test-Path -LiteralPath $adminIndex -PathType Leaf)
if (-not $needsBuild) {
    $needsBuild = (Get-Item -LiteralPath $adminIndex).LastWriteTimeUtc -lt $latestInput
}
if ($needsBuild) {
    Push-Location $adminRoot
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "RotoWeaveServer admin build failed." }
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

& $python -c "import PIL, pystray"
if ($LASTEXITCODE -ne 0) {
    throw "RotoWeaveServer tray dependencies are missing. Run Setup.cmd again."
}
$serverDataRoot = (& $python -c "from server.launcher import data_root; print(data_root())").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($serverDataRoot)) {
    throw "Unable to resolve the RotoWeaveServer data directory."
}
$pidMarker = Join-Path $serverDataRoot "server.pid"

& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $stopScript
if ($LASTEXITCODE -ne 0) {
    throw "Unable to stop the existing RotoWeave server."
}

$serverProcess = Start-Process `
    -FilePath $python `
    -ArgumentList @("-m", "server.launcher") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while ([DateTime]::UtcNow -lt $deadline) {
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) {
        throw "RotoWeaveServer exited during startup with code $($serverProcess.ExitCode). Check the launcher log."
    }
    if (Test-Path -LiteralPath $pidMarker -PathType Leaf) { break }
    Start-Sleep -Milliseconds 250
}
if (-not (Test-Path -LiteralPath $pidMarker -PathType Leaf)) {
    Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
    throw "RotoWeaveServer did not become ready within 30 seconds. Check the launcher log."
}
Write-Host "RotoWeaveServer started in the background. Use the notification-area icon to open or exit it."
exit 0
