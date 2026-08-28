param([switch]$EnvironmentOnly, [string]$OfflinePayloadRoot)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$contractsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveContracts"))
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not $EnvironmentOnly) {
    & (Join-Path $projectRoot "..\scripts\Setup-RotoWeave.ps1") -Role Client
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    & py.exe -3.12 -m venv (Join-Path $projectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the RotoWeaveClient Python 3.12 environment." }
}

$pipArguments = @("-m", "pip", "install", "--disable-pip-version-check")
$wheelhouse = if ([string]::IsNullOrWhiteSpace($OfflinePayloadRoot)) { $null } else { Join-Path $OfflinePayloadRoot "client-python\wheelhouse" }
if ($wheelhouse -and (Test-Path -LiteralPath $wheelhouse -PathType Container)) { $pipArguments += @("--no-index", "--find-links", $wheelhouse) }
$pipArguments += @("-r", (Join-Path $projectRoot "requirements-win-lock.txt"))
& $python @pipArguments
if ($LASTEXITCODE -ne 0) { throw "RotoWeaveClient Python dependency installation failed." }
$contractsArguments = @("-m", "pip", "install", "--disable-pip-version-check")
if ($wheelhouse -and (Test-Path -LiteralPath $wheelhouse -PathType Container)) { $contractsArguments += @("--no-index", "--find-links", $wheelhouse) }
$contractsArguments += @("-r", (Join-Path $contractsRoot "build-requirements-lock.txt"))
& $python @contractsArguments
if ($LASTEXITCODE -ne 0) { throw "The locked RotoWeaveContracts build backend installation failed." }
$editableArguments = @("-m", "pip", "install", "--disable-pip-version-check", "--no-build-isolation")
if ($wheelhouse -and (Test-Path -LiteralPath $wheelhouse -PathType Container)) { $editableArguments += @("--no-index", "--find-links", $wheelhouse) }
$editableArguments += @("-e", $contractsRoot)
& $python @editableArguments
if ($LASTEXITCODE -ne 0) { throw "The local RotoWeaveContracts installation failed." }

Push-Location $projectRoot
try {
    $npmArguments = @("ci")
    $npmCache = if ([string]::IsNullOrWhiteSpace($OfflinePayloadRoot)) { $null } else { Join-Path $OfflinePayloadRoot "client-node\npm-cache" }
    if ($npmCache -and (Test-Path -LiteralPath $npmCache -PathType Container)) { $npmArguments += @("--offline", "--cache", $npmCache) }
    & npm.cmd @npmArguments
    if ($LASTEXITCODE -ne 0) { throw "RotoWeaveClient Node dependency installation failed." }
} finally {
    Pop-Location
}

Write-Host "RotoWeaveClient environment is ready."
