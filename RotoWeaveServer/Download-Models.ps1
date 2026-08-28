param(
    [Parameter(Mandatory = $true)]
    [string]$Sources,
    [string[]]$Role
)
$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$contractsRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveContracts"))
if (-not [string]::IsNullOrWhiteSpace($env:ROTOWEAVE_MODELS_ROOT) -and
    -not [string]::IsNullOrWhiteSpace($env:AIFRAME_MODELS_ROOT) -and
    $env:ROTOWEAVE_MODELS_ROOT.Trim() -ne $env:AIFRAME_MODELS_ROOT.Trim()) {
    throw "ROTOWEAVE_MODELS_ROOT 与兼容变量 AIFRAME_MODELS_ROOT 同时存在且值冲突。"
}
$configuredModelsRoot = if (-not [string]::IsNullOrWhiteSpace($env:ROTOWEAVE_MODELS_ROOT)) { $env:ROTOWEAVE_MODELS_ROOT } else { $env:AIFRAME_MODELS_ROOT }
$modelsRoot = if ([string]::IsNullOrWhiteSpace($configuredModelsRoot)) {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot "..\RotoWeaveModels"))
} else {
    [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($configuredModelsRoot))
}
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "RotoWeaveServer environment is missing. Run $projectRoot\Setup.cmd first."
}
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) { $contractsRoot } else { "$contractsRoot;$previousPythonPath" }
    $arguments = @(
        (Join-Path $projectRoot "scripts\download-independent-models.py"),
        "--sources", [System.IO.Path]::GetFullPath($Sources),
        "--models-root", $modelsRoot
    )
    foreach ($item in $Role) { $arguments += @("--role", $item) }
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Independent model download failed with exit code $LASTEXITCODE." }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
