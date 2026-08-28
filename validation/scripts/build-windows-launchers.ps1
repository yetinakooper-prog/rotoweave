param(
    [switch]$SkipTests,
    [string]$PythonEnvironment = ".venv",
    [string]$OutputDirectory = "release\launchers"
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$workspace = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "RotoWeaveClient"))
$validationScripts = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "validation\scripts"))
$environmentRoot = if ([System.IO.Path]::IsPathRooted($PythonEnvironment)) {
    [System.IO.Path]::GetFullPath($PythonEnvironment)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $workspace $PythonEnvironment))
}
$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $workspace $OutputDirectory))
}
$allowedOutputRoot = [System.IO.Path]::GetFullPath((Join-Path $workspace "release\launchers"))
if (-not $outputRoot.Equals($allowedOutputRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Launcher output must be the dedicated release/launchers directory: $allowedOutputRoot"
}
$python = Join-Path $environmentRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Missing launcher build Python environment: $python"
}
$contractsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "RotoWeaveContracts"))
$pythonPathEntries = @($contractsRoot)
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) { $pythonPathEntries += $env:PYTHONPATH }
$env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator

$node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $node -and -not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $candidate = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $node = Get-Item -LiteralPath $candidate }
}
if (-not $node) { throw "Node.js 22+ is required." }
$nodePath = if ($node.Source) { $node.Source } else { $node.FullName }

Set-Location -LiteralPath $workspace
& $python (Join-Path $validationScripts "validate-product-contract.py")
if ($LASTEXITCODE -ne 0) { throw "Product contract validation failed." }
& $python (Join-Path $validationScripts "validate-release-boundary.py")
if ($LASTEXITCODE -ne 0) { throw "Release boundary validation failed." }
& $nodePath node_modules\typescript\bin\tsc --noEmit
if ($LASTEXITCODE -ne 0) { throw "TypeScript validation failed." }
& $nodePath node_modules\vite\bin\vite.js build
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
if (-not $SkipTests) {
    & $python -m pytest backend\tests -q
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed." }
    $frontendTests = @(
        Get-ChildItem -LiteralPath (Join-Path $workspace "tests") -Filter "*.test.mjs" -File |
            Sort-Object Name |
            ForEach-Object { $_.FullName }
    )
    & $nodePath --test @frontendTests
    if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed." }
}

$runningClients = @(Get-Process -Name "RotoWeave-Client" -ErrorAction SilentlyContinue)
if ($runningClients.Count -gt 0) {
    $runningIds = ($runningClients | ForEach-Object { [string]$_.Id }) -join ", "
    throw "RotoWeave Client is running (PID: $runningIds). Run RotoWeaveClient\Stop.cmd before building; the existing release was not modified."
}

$runToken = [Guid]::NewGuid().ToString("N")
$buildRoot = Join-Path $workspace "Temp\LauncherBuild"
$workRoot = Join-Path $buildRoot "work-$runToken"
$stagingRoot = Join-Path $buildRoot "dist-$runToken"
& $python -m PyInstaller --noconfirm --clean --distpath $stagingRoot --workpath $workRoot RotoWeaveClient.spec
if ($LASTEXITCODE -ne 0) { throw "Client launcher build failed." }

$stagedClientRoot = Join-Path $stagingRoot "RotoWeave-Client"
$retiredCombinedServerRoot = Join-Path $outputRoot "RotoWeave-Server"
if (Test-Path -LiteralPath $retiredCombinedServerRoot) {
    throw "Retired combined server package remains in the client release root: $retiredCombinedServerRoot"
}
Copy-Item -LiteralPath (Join-Path $projectRoot "Docs\CLIENT_LAUNCHER.zh-CN.md") -Destination (Join-Path $stagedClientRoot "README-CLIENT.zh-CN.md") -Force
$validation = & $python (Join-Path $validationScripts "validate-launcher-packages.py") --client $stagedClientRoot
if ($LASTEXITCODE -ne 0) { throw "Client package boundary validation failed." }

$clientRoot = Join-Path $outputRoot "RotoWeave-Client"
$clientExecutable = Join-Path $clientRoot "RotoWeave-Client.exe"
$stagedClientExecutable = Join-Path $stagedClientRoot "RotoWeave-Client.exe"
$validationObject = $validation | ConvertFrom-Json
$validationObject.client.executable = "RotoWeave-Client/RotoWeave-Client.exe"
$manifest = [ordered]@{
    schemaVersion = 1
    productVersion = "4.0.0"
    createdAtUtc = [DateTime]::UtcNow.ToString("o")
    client = [ordered]@{
        path = "RotoWeave-Client/RotoWeave-Client.exe"
        sha256 = (Get-FileHash -LiteralPath $stagedClientExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    validation = $validationObject
}
$stagedManifest = Join-Path $stagingRoot "CLIENT-MANIFEST.json"
[System.IO.File]::WriteAllText(
    $stagedManifest,
    ($manifest | ConvertTo-Json -Depth 10) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$backupRoot = Join-Path $outputRoot ".RotoWeave-Client.backup-$runToken"
$manifestPath = Join-Path $outputRoot "CLIENT-MANIFEST.json"
$manifestBackup = Join-Path $stagingRoot "CLIENT-MANIFEST.previous.json"
$hadClient = Test-Path -LiteralPath $clientRoot
$hadManifest = Test-Path -LiteralPath $manifestPath -PathType Leaf
try {
    if ($hadManifest) {
        Copy-Item -LiteralPath $manifestPath -Destination $manifestBackup
    }
    if ($hadClient) {
        Move-Item -LiteralPath $clientRoot -Destination $backupRoot
    }
    Move-Item -LiteralPath $stagedClientRoot -Destination $clientRoot
    Copy-Item -LiteralPath $stagedManifest -Destination $manifestPath -Force
    $finalValidation = & $python (Join-Path $validationScripts "validate-launcher-packages.py") --client $clientRoot
    if ($LASTEXITCODE -ne 0) { throw "Promoted client package validation failed." }
} catch {
    $promotionError = $_
    if ((Test-Path -LiteralPath $clientRoot) -and -not (Test-Path -LiteralPath $stagedClientRoot)) {
        Move-Item -LiteralPath $clientRoot -Destination $stagedClientRoot
    }
    if (Test-Path -LiteralPath $backupRoot) {
        Move-Item -LiteralPath $backupRoot -Destination $clientRoot
    }
    if ($hadManifest -and (Test-Path -LiteralPath $manifestBackup -PathType Leaf)) {
        Copy-Item -LiteralPath $manifestBackup -Destination $manifestPath -Force
    } elseif ((-not $hadManifest) -and (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        Remove-Item -LiteralPath $manifestPath -Force
    }
    throw $promotionError
}
if (Test-Path -LiteralPath $backupRoot) {
    Remove-Item -LiteralPath $backupRoot -Recurse -Force
}
Write-Host "Client launcher: $clientExecutable"
