param(
    [switch]$SkipTests,
    [string]$PythonEnvironment = "..\.venv",
    [string]$OutputDirectory = "release\server-only"
)

$ErrorActionPreference = "Stop"
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $workspace ".."))
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
$allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $workspace "release\server-only"))
if (-not $outputRoot.Equals($allowedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Server-only output must be release/server-only: $allowedRoot"
}
$python = Join-Path $environmentRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Missing build Python: $python" }
$contractsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "RotoWeaveContracts"))
$pythonPathEntries = @($contractsRoot)
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) { $pythonPathEntries += $env:PYTHONPATH }
$env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator
$node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $node) {
    $candidate = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $node = Get-Item -LiteralPath $candidate }
}
if (-not $node) { throw "Node.js 22+ is required." }
$nodePath = if ($node.Source) { $node.Source } else { $node.FullName }
$npmCandidate = Join-Path (Split-Path -Parent $nodePath) "npm.cmd"
$npm = if (Test-Path -LiteralPath $npmCandidate -PathType Leaf) {
    $npmCandidate
} else {
    (Get-Command npm.cmd -ErrorAction Stop).Source
}

Set-Location -LiteralPath $workspace
& $python (Join-Path $validationScripts "validate-product-contract.py")
if ($LASTEXITCODE -ne 0) { throw "Product contract validation failed." }
& $python (Join-Path $validationScripts "validate-release-boundary.py")
if ($LASTEXITCODE -ne 0) { throw "Release boundary validation failed." }
$adminRoot = Join-Path $workspace "server-admin"
Push-Location -LiteralPath $adminRoot
try {
    & $npm run build
    if ($LASTEXITCODE -ne 0) { throw "Server admin build failed." }
} finally {
    Pop-Location
}
if (-not $SkipTests) {
    & $python -m pytest `
        (Join-Path $projectRoot "validation\tests\test_remote_server_v4.py") `
        (Join-Path $workspace "tests\test_server_admin_v4.py") -q
    if ($LASTEXITCODE -ne 0) { throw "Server tests failed." }
}

$runningServers = @(Get-Process -Name "RotoWeave-Server" -ErrorAction SilentlyContinue)
if ($runningServers.Count -gt 0) {
    $runningIds = ($runningServers | ForEach-Object { [string]$_.Id }) -join ", "
    throw "RotoWeave Server is running (PID: $runningIds). Run RotoWeaveServer\Stop.cmd before building; the existing release was not modified."
}

$runToken = [Guid]::NewGuid().ToString("N")
$buildRoot = Join-Path $workspace "Temp\ServerOnlyBuild"
$workRoot = Join-Path $buildRoot "work-$runToken"
$stagingRoot = Join-Path $buildRoot "dist-$runToken"
$serverRuntimeStage = Join-Path $buildRoot "runtimes-$runToken"
& $python scripts\prepare-server-runtimes.py --output $serverRuntimeStage
if ($LASTEXITCODE -ne 0) { throw "Fixed High/Ultra runtime staging failed." }
$env:ROTOWEAVE_SERVER_RUNTIMES_STAGE = $serverRuntimeStage
try {
    & $python -m PyInstaller --noconfirm --clean --distpath $stagingRoot --workpath $workRoot RotoWeaveServer.spec
} finally {
    $env:ROTOWEAVE_SERVER_RUNTIMES_STAGE = $null
}
if ($LASTEXITCODE -ne 0) { throw "Server-only build failed." }
$stagedServerRoot = Join-Path $stagingRoot "RotoWeave-Server"
Copy-Item -LiteralPath (Join-Path $workspace "README.md") -Destination (Join-Path $stagedServerRoot "README-SERVER.zh-CN.md") -Force
$validation = & $python (Join-Path $validationScripts "validate-launcher-packages.py") --server $stagedServerRoot
if ($LASTEXITCODE -ne 0) { throw "Server package boundary validation failed." }
$serverRoot = Join-Path $outputRoot "RotoWeave-Server"
$serverExecutable = Join-Path $serverRoot "RotoWeave-Server.exe"
$stagedServerExecutable = Join-Path $stagedServerRoot "RotoWeave-Server.exe"
$validationObject = $validation | ConvertFrom-Json
$validationObject.server.executable = "RotoWeave-Server/RotoWeave-Server.exe"
$manifest = [ordered]@{
    schemaVersion = 1
    productVersion = "4.0.0"
    createdAtUtc = [DateTime]::UtcNow.ToString("o")
    server = [ordered]@{
        path = "RotoWeave-Server/RotoWeave-Server.exe"
        sha256 = (Get-FileHash -LiteralPath $stagedServerExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
        modelWeightsIncluded = $false
        fixedRuntimes = @("rotoweave-high-runtime-v1", "rotoweave-ultra-runtime-v1")
    }
    validation = $validationObject
}
$stagedManifest = Join-Path $stagingRoot "SERVER-MANIFEST.json"
[System.IO.File]::WriteAllText(
    $stagedManifest,
    ($manifest | ConvertTo-Json -Depth 10) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$backupRoot = Join-Path $outputRoot ".RotoWeave-Server.backup-$runToken"
$manifestPath = Join-Path $outputRoot "SERVER-MANIFEST.json"
$manifestBackup = Join-Path $stagingRoot "SERVER-MANIFEST.previous.json"
$hadServer = Test-Path -LiteralPath $serverRoot
$hadManifest = Test-Path -LiteralPath $manifestPath -PathType Leaf
try {
    if ($hadManifest) {
        Copy-Item -LiteralPath $manifestPath -Destination $manifestBackup
    }
    if ($hadServer) {
        Move-Item -LiteralPath $serverRoot -Destination $backupRoot
    }
    Move-Item -LiteralPath $stagedServerRoot -Destination $serverRoot
    Copy-Item -LiteralPath $stagedManifest -Destination $manifestPath -Force
    $finalValidation = & $python (Join-Path $validationScripts "validate-launcher-packages.py") --server $serverRoot
    if ($LASTEXITCODE -ne 0) { throw "Promoted server package validation failed." }
} catch {
    $promotionError = $_
    if ((Test-Path -LiteralPath $serverRoot) -and -not (Test-Path -LiteralPath $stagedServerRoot)) {
        Move-Item -LiteralPath $serverRoot -Destination $stagedServerRoot
    }
    if (Test-Path -LiteralPath $backupRoot) {
        Move-Item -LiteralPath $backupRoot -Destination $serverRoot
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
Write-Host "Server-only package: $serverRoot"
