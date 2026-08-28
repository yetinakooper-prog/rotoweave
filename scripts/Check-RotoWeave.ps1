param(
    [ValidateSet("Client", "Server", "All")]
    [string]$Role = "Client",
    [switch]$FullHash,
    [switch]$StrictProfiles,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Bootstrap-Common.ps1")
try {
    $paths = Get-RotoWeaveBootstrapPaths
    $normalizedRole = Resolve-RotoWeaveRole $Role
    $pythonLauncher = Get-RotoWeaveBootstrapPython
    $arguments = @($paths.Bootstrap, "--project-root", $paths.ProjectRoot, "check", "--role", $normalizedRole)
    if ($FullHash) { $arguments += "--full-hash" }
    if ($StrictProfiles) { $arguments += "--strict-profiles" }
    if ($Json) { $arguments += "--json" }
    & $pythonLauncher @arguments
    exit $LASTEXITCODE
} catch {
    Write-Error $_
    exit 2
}
