param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [ValidateSet("Client", "Server", "All")]
    [string]$Role = "Client"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Bootstrap-Common.ps1")
$paths = Get-RotoWeaveBootstrapPaths
$normalizedRole = Resolve-RotoWeaveRole $Role
$pythonLauncher = Get-RotoWeaveBootstrapPython
Assert-RotoWeaveNode
$arguments = @(
    $paths.Bootstrap,
    "--project-root", $paths.ProjectRoot,
    "export-bundle", "--role", $normalizedRole,
    "--output-directory", [System.IO.Path]::GetFullPath($OutputDirectory),
    "--json-progress"
)
& $pythonLauncher @arguments
if ($LASTEXITCODE -ne 0) { throw "Deployment ZIP export failed." }
