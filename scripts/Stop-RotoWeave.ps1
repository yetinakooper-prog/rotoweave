param(
    [ValidateSet("Client", "Server", "All")]
    [string]$Role = "Client"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Bootstrap-Common.ps1")
$paths = Get-RotoWeaveBootstrapPaths
$normalizedRole = Resolve-RotoWeaveRole $Role
if ($normalizedRole -in @("client", "all")) {
    & (Join-Path $paths.ProjectRoot "RotoWeaveClient\Stop.ps1")
}
if ($normalizedRole -in @("server", "all")) {
    & (Join-Path $paths.ProjectRoot "RotoWeaveServer\scripts\stop-rotoweave-server.ps1")
}
Write-Host "Stop checks completed."
