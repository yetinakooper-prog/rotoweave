param(
    [int]$ApiPort = 8766
)

$ErrorActionPreference = "Stop"
$candidateIds = [System.Collections.Generic.HashSet[int]]::new()
foreach ($clientProcess in (Get-Process -Name "RotoWeave-Client" -ErrorAction SilentlyContinue)) {
    [void]$candidateIds.Add([int]$clientProcess.Id)
}
$listeners = Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    if ([int]$listener.OwningProcess -gt 0) {
        [void]$candidateIds.Add([int]$listener.OwningProcess)
    }
}

if ($candidateIds.Count -eq 0) {
    Write-Host "RotoWeave 4.0 client is not running."
    exit 0
}

$stopped = 0
$rejected = 0
foreach ($clientProcessId in $candidateIds) {
    $process = Get-Process -Id $clientProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    $processName = [string]$process.ProcessName
    $commandLine = ""
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $clientProcessId" -ErrorAction Stop
        $commandLine = [string]$processInfo.CommandLine
    } catch {
        # Exact packaged process name remains sufficient for standard-user shutdown.
    }
    $isClient = $processName -ieq "RotoWeave-Client" -or
        $commandLine -match '(?i)RotoWeave-Client\.exe' -or
        $commandLine -match '(?i)backend[\\/]client_launcher\.py' -or
        $commandLine -match '(?i)-m\s+backend\.client_launcher(?:\s|$)' -or
        $commandLine -match '(?i)-m\s+backend\.app\.main(?:\s|$)'
    if (-not $isClient) {
        Write-Error "Refusing to stop a non-RotoWeave listener: PID $clientProcessId, $processName"
        $rejected += 1
        continue
    }
    Write-Host "Stopping RotoWeave 4.0 client: PID $clientProcessId"
    Stop-Process -Id $clientProcessId -ErrorAction SilentlyContinue
    for ($attempt = 0; $attempt -lt 20; $attempt += 1) {
        if ($null -eq (Get-Process -Id $clientProcessId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($null -ne (Get-Process -Id $clientProcessId -ErrorAction SilentlyContinue)) {
        Write-Warning "Client did not exit within 5 seconds; forcing the client process to stop."
        Stop-Process -Id $clientProcessId -Force -ErrorAction SilentlyContinue
        for ($attempt = 0; $attempt -lt 20; $attempt += 1) {
            if ($null -eq (Get-Process -Id $clientProcessId -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 100
        }
    }
    $stopped += 1
}

$remaining = $null
for ($attempt = 0; $attempt -lt 20; $attempt += 1) {
    $remaining = Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction SilentlyContinue
    if (-not $remaining) { break }
    Start-Sleep -Milliseconds 100
}
if ($remaining) {
    Write-Error "Client API port remains occupied: $ApiPort."
    exit 2
}
if ($stopped -gt 0) {
    Write-Host "RotoWeave 4.0 client stopped."
} elseif ($rejected -eq 0) {
    Write-Host "RotoWeave 4.0 client is not running."
}
exit 0
