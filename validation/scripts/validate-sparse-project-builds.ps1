param(
    [string]$EvidenceDirectory = "Temp\Codex\SparseBuilds\physical-split-20260823",
    [switch]$KeepStage
)

$ErrorActionPreference = "Stop"
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$evidenceRoot = if ([System.IO.Path]::IsPathRooted($EvidenceDirectory)) {
    [System.IO.Path]::GetFullPath($EvidenceDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $workspace $EvidenceDirectory))
}
$workspacePrefix = $workspace + [System.IO.Path]::DirectorySeparatorChar
if (-not $evidenceRoot.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Sparse-build evidence must stay inside the workspace."
}

$stage = Join-Path $evidenceRoot ("stage-" + [Guid]::NewGuid().ToString("N"))
$clientStage = Join-Path $stage "client-only"
$serverStage = Join-Path $stage "server-only"
$python = Join-Path $workspace ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Governance Python environment is missing: $python"
}

function Copy-Tree([string]$Source, [string]$Target) {
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    & robocopy $Source $Target /E /XD .venv node_modules runtime dist __pycache__ .pytest_cache .mypy_cache .ruff_cache /XF *.pyc | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "robocopy failed for $Source with exit code $LASTEXITCODE" }
}

try {
    New-Item -ItemType Directory -Path $clientStage,$serverStage -Force | Out-Null
    Copy-Tree (Join-Path $workspace "RotoWeaveContracts") (Join-Path $clientStage "RotoWeaveContracts")
    Copy-Tree (Join-Path $workspace "RotoWeaveClient") (Join-Path $clientStage "RotoWeaveClient")
    Copy-Tree (Join-Path $workspace "RotoWeaveContracts") (Join-Path $serverStage "RotoWeaveContracts")
    Copy-Tree (Join-Path $workspace "RotoWeaveServer") (Join-Path $serverStage "RotoWeaveServer")

    if (Test-Path -LiteralPath (Join-Path $clientStage "RotoWeaveServer")) { throw "Client sparse copy contains Server source." }
    if (Test-Path -LiteralPath (Join-Path $serverStage "RotoWeaveClient")) { throw "Server sparse copy contains Client source." }

    Push-Location (Join-Path $clientStage "RotoWeaveClient")
    try {
        npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw "Sparse Client npm ci failed." }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Sparse Client build failed." }
    } finally { Pop-Location }

    Push-Location (Join-Path $serverStage "RotoWeaveServer\server-admin")
    try {
        npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw "Sparse Server Admin npm ci failed." }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Sparse Server Admin build failed." }
    } finally { Pop-Location }

    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = @((Join-Path $clientStage "RotoWeaveClient"), (Join-Path $clientStage "RotoWeaveContracts")) -join [System.IO.Path]::PathSeparator
        & $python -c "import backend.app.main, contracts.remote_protocol; print('client-only Python imports passed')"
        if ($LASTEXITCODE -ne 0) { throw "Sparse Client Python import failed." }

        $env:PYTHONPATH = @((Join-Path $serverStage "RotoWeaveServer"), (Join-Path $serverStage "RotoWeaveContracts")) -join [System.IO.Path]::PathSeparator
        & $python -c "import server.api, server.service, worker.cuda_matting.rotoweave_adapter, contracts.remote_protocol; print('server-only Python imports passed')"
        if ($LASTEXITCODE -ne 0) { throw "Sparse Server Python import failed." }
    } finally { $env:PYTHONPATH = $previousPythonPath }

    $evidence = [ordered]@{
        schemaVersion = 2
        productVersion = "4.0.0"
        createdAtUtc = [DateTime]::UtcNow.ToString("o")
        clientSourceSet = @("RotoWeaveClient", "RotoWeaveContracts")
        serverSourceSet = @("RotoWeaveServer", "RotoWeaveContracts")
        clientContainsServer = $false
        serverContainsClient = $false
        clientNpmCi = "passed"
        clientProductionBuild = "passed"
        clientPythonImports = "passed"
        serverAdminNpmCi = "passed"
        serverAdminProductionBuild = "passed"
        serverPythonImports = "passed"
    }
    $evidencePath = Join-Path $evidenceRoot "SPARSE-PROJECT-BUILD-EVIDENCE.json"
    [System.IO.File]::WriteAllText($evidencePath, ($evidence | ConvertTo-Json -Depth 8) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Sparse project build evidence: $evidencePath"
}
finally {
    $resolvedStage = [System.IO.Path]::GetFullPath($stage)
    $evidencePrefix = [System.IO.Path]::GetFullPath($evidenceRoot) + [System.IO.Path]::DirectorySeparatorChar
    $stageLeaf = Split-Path -Leaf $resolvedStage
    if (-not $KeepStage -and $resolvedStage.StartsWith($evidencePrefix, [System.StringComparison]::OrdinalIgnoreCase) -and $stageLeaf.StartsWith("stage-", [System.StringComparison]::Ordinal)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
