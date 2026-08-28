param(
    [string]$DataRoot = ""
)

$ErrorActionPreference = "Stop"

$serverDataRoot = if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    Join-Path $env:LOCALAPPDATA "RotoWeave-4.0-Server"
} else {
    [System.IO.Path]::GetFullPath($DataRoot)
}
$configPath = Join-Path $serverDataRoot "server-launcher.json"
$pidPath = Join-Path $serverDataRoot "server.pid"
$ports = @(8443, 8444)

function Invoke-TaskKill {
    param(
        [Parameter(Mandatory = $true)]
        [int]$TargetProcessId,
        [switch]$Force
    )

    # taskkill writes ordinary process-tree races to stderr. With the script's
    # ErrorActionPreference=Stop, invoking it directly turns that diagnostic
    # into a terminating PowerShell error before we can verify the real state.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Join-Path $env:SystemRoot "System32\taskkill.exe"
    $startInfo.Arguments = "/PID $TargetProcessId /T" + $(if ($Force) { " /F" } else { "" })
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $runner = [System.Diagnostics.Process]::new()
    $runner.StartInfo = $startInfo
    try {
        [void]$runner.Start()
        $standardOutput = $runner.StandardOutput.ReadToEnd()
        $standardError = $runner.StandardError.ReadToEnd()
        $runner.WaitForExit()
        return [pscustomobject]@{
            ExitCode = [int]$runner.ExitCode
            Output = [string]$standardOutput
            Error = [string]$standardError
        }
    } finally {
        $runner.Dispose()
    }
}

function Wait-ProcessExit {
    param(
        [Parameter(Mandatory = $true)]
        [int]$TargetProcessId,
        [int]$Attempts = 20
    )

    for ($attempt = 0; $attempt -lt $Attempts; $attempt += 1) {
        if ($null -eq (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    try {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        $ports = @([int]$config.apiPort, [int]$config.adminPort) | Select-Object -Unique
    } catch {
        Write-Warning "Server config is invalid; checking default ports 8443/8444."
    }
}

$candidateIds = [System.Collections.Generic.HashSet[int]]::new()
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    try {
        $pidRecord = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
        if ([int]$pidRecord.pid -gt 0) {
            [void]$candidateIds.Add([int]$pidRecord.pid)
        }
    } catch {
        Write-Warning "Server PID marker is invalid; checking listener ports."
    }
}

$listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in $ports }
foreach ($listener in $listeners) {
    if ([int]$listener.OwningProcess -gt 0) {
        [void]$candidateIds.Add([int]$listener.OwningProcess)
    }
}
foreach ($packagedProcess in (Get-Process -Name "RotoWeave-Server" -ErrorAction SilentlyContinue)) {
    [void]$candidateIds.Add([int]$packagedProcess.Id)
}

if ($candidateIds.Count -eq 0) {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Write-Host "RotoWeave 4.0 remote server is not running."
    exit 0
}

$stopped = 0
$rejected = 0
foreach ($serverProcessId in $candidateIds) {
    $process = Get-Process -Id $serverProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        continue
    }
    $processName = [string]$process.ProcessName
    $commandLine = ""
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverProcessId" -ErrorAction Stop
        $commandLine = [string]$processInfo.CommandLine
    } catch {
        # Standard users may not read another process command line. The exact packaged process name remains sufficient.
    }
    $isServer = $processName -ieq "RotoWeave-Server" -or
        $commandLine -match '(?i)RotoWeave-Server\.exe' -or
        $commandLine -match '(?i)-m\s+server(?:\.launcher)?(?:\s|$)'
    if (-not $isServer) {
        Write-Error "Refusing to stop a non-RotoWeave listener: PID $serverProcessId, $processName"
        $rejected += 1
        continue
    }

    Write-Host "Stopping RotoWeave 4.0 remote server: PID $serverProcessId"
    $gracefulResult = Invoke-TaskKill -TargetProcessId $serverProcessId
    $exited = Wait-ProcessExit -TargetProcessId $serverProcessId
    if (-not $exited) {
        Write-Warning "Server did not exit within 5 seconds; terminating its process tree."
        $forceResult = Invoke-TaskKill -TargetProcessId $serverProcessId -Force
        $exited = Wait-ProcessExit -TargetProcessId $serverProcessId -Attempts 8
    }
    if (-not $exited) {
        # A PyInstaller parent can remain after taskkill reports a child-tree
        # race. The identity was already checked above, so this fallback is
        # still scoped to the validated RotoWeave server process.
        Stop-Process -Id $serverProcessId -Force -ErrorAction SilentlyContinue
        $exited = Wait-ProcessExit -TargetProcessId $serverProcessId -Attempts 8
    }
    if (-not $exited) {
        $detail = if ($null -ne $forceResult -and $forceResult.Error) {
            $forceResult.Error.Trim()
        } elseif ($gracefulResult.Error) {
            $gracefulResult.Error.Trim()
        } else {
            "unknown process termination failure"
        }
        Write-Error "Server process remains active: PID $serverProcessId. $detail"
        exit 2
    }
    $stopped += 1
}

$remaining = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in $ports }
if ($remaining) {
    Write-Error "Server ports remain occupied: $($remaining.LocalPort -join ', ')."
    exit 2
}

if (Test-Path -LiteralPath $pidPath) {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction Stop
}
if ($stopped -gt 0) {
    Write-Host "RotoWeave 4.0 remote server stopped."
} elseif ($rejected -eq 0) {
    Write-Host "RotoWeave 4.0 remote server is not running."
}
exit 0
