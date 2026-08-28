param(
    [ValidateSet("Client", "Server", "All")]
    [string]$Role = "Client"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Bootstrap-Common.ps1")
$paths = Get-RotoWeaveBootstrapPaths
$normalizedRole = Resolve-RotoWeaveRole $Role
$pythonLauncher = Get-RotoWeaveBootstrapPython
$checkArguments = @($paths.Bootstrap, "--project-root", $paths.ProjectRoot, "check", "--role", $normalizedRole)
& $pythonLauncher @checkArguments
if ($LASTEXITCODE -ne 0) {
    throw "This device is not ready. Run Setup-RotoWeave.cmd $Role first."
}

if ($normalizedRole -eq "client") {
    & (Join-Path $paths.ProjectRoot "RotoWeaveClient\Start.ps1")
    exit $LASTEXITCODE
}
if ($normalizedRole -eq "server") {
    & (Join-Path $paths.ProjectRoot "RotoWeaveServer\Start.ps1")
    exit $LASTEXITCODE
}

$serverStart = Join-Path $paths.ProjectRoot "RotoWeaveServer\Start.ps1"
if (-not (Test-RotoWeaveTcpPort -HostName "127.0.0.1" -Port 8444)) {
    $serverArguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$serverStart`""
    $serverProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $serverArguments -WindowStyle Hidden -PassThru
    $serverProcess.WaitForExit()
    if ($serverProcess.ExitCode -ne 0) { throw "RotoWeaveServer background startup failed, exit=$($serverProcess.ExitCode)." }
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-RotoWeaveTcpPort -HostName "127.0.0.1" -Port 8444) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-RotoWeaveTcpPort -HostName "127.0.0.1" -Port 8444)) {
        throw "RotoWeaveServer did not open localhost admin port 8444 within 90 seconds."
    }
    Write-Host "RotoWeaveServer is ready in the background."
} else {
    Write-Host "RotoWeaveServer is already running; duplicate startup was skipped."
}
& (Join-Path $paths.ProjectRoot "RotoWeaveClient\Start.ps1")
exit $LASTEXITCODE
