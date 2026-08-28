$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Resolve-RotoWeaveRole {
    param([string]$Role)
    $value = if ([string]::IsNullOrWhiteSpace($Role)) { "Client" } else { $Role }
    switch ($value.ToLowerInvariant()) {
        "client" { return "client" }
        "server" { return "server" }
        "all" { return "all" }
        default { throw "Role must be Client, Server, or All." }
    }
}

function Get-RotoWeaveSourceCatalog {
    $paths = Get-RotoWeaveBootstrapPaths
    $catalogPath = Join-Path $paths.ProjectRoot "RotoWeaveContracts\deployment-sources.json"
    if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) {
        throw "Controlled deployment source catalog is missing: $catalogPath"
    }
    $catalog = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($catalog.schemaVersion -ne 1 -or $catalog.platform -ne "windows-x64") {
        throw "Controlled deployment source catalog schema/platform is unsupported."
    }
    return $catalog
}

function Confirm-RotoWeaveAction {
    param([string]$Message, [switch]$Approved, [switch]$NonInteractive)
    if ($Approved) { return $true }
    if ($NonInteractive) { return $false }
    $answer = Read-Host "$Message [y/N]"
    return $answer.Trim().ToLowerInvariant() -in @("y", "yes", "是")
}

function Invoke-RotoWeaveVerifiedDownload {
    param([pscustomobject]$Source, [string]$Destination)
    $partial = "$Destination.partial"
    New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($Destination)) | Out-Null
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { throw "curl.exe is required for controlled host-tool download." }
    & $curl.Source -L --fail --retry 2 -C - --output $partial ([string]$Source.url)
    if ($LASTEXITCODE -ne 0) { throw "Controlled download failed; partial file retained: $partial" }
    $item = Get-Item -LiteralPath $partial
    if ($item.Length -ne [long]$Source.bytes) { throw "Downloaded byte count mismatch for $($Source.id)." }
    $actual = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$Source.sha256).ToLowerInvariant()) { throw "Downloaded SHA-256 mismatch for $($Source.id)." }
    if (Test-Path -LiteralPath $Destination) { throw "Controlled download target already exists: $Destination" }
    Move-Item -LiteralPath $partial -Destination $Destination
}

function Export-RotoWeavechainFromBundle {
    param([string]$BundlePath, [pscustomobject]$Source, [string]$Destination)
    if ([string]::IsNullOrWhiteSpace($BundlePath)) { return $false }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead([System.IO.Path]::GetFullPath($BundlePath))
    try {
        $suffix = "/$([string]$Source.filename)"
        $entries = @($archive.Entries | Where-Object { -not [string]::IsNullOrEmpty($_.Name) -and $_.FullName.Replace("\", "/").EndsWith($suffix, [System.StringComparison]::OrdinalIgnoreCase) })
        if ($entries.Count -eq 0) { return $false }
        if ($entries.Count -ne 1) { throw "Bundle contains duplicate host toolchain entries for $($Source.filename)." }
        New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($Destination)) | Out-Null
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], $Destination, $false)
    } finally {
        $archive.Dispose()
    }
    $item = Get-Item -LiteralPath $Destination
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($item.Length -ne [long]$Source.bytes -or $actual -ne ([string]$Source.sha256).ToLowerInvariant()) {
        Remove-Item -LiteralPath $Destination -Force
        throw "Bundle host toolchain verification failed for $($Source.id)."
    }
    return $true
}

function Assert-RotoWeaveAuthenticode {
    param([string]$Path, [string]$Publisher)
    if ([string]::IsNullOrWhiteSpace($Publisher)) { return }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notlike "*$Publisher*") {
        throw "Authenticode publisher validation failed: $Path"
    }
}

function Find-RotoWeavePythonLauncher {
    $command = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $knownLauncher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
    if (Test-Path -LiteralPath $knownLauncher -PathType Leaf) { return $knownLauncher }
    return $null
}

function Test-RotoWeavePython312Available {
    param([string]$LauncherPath)
    if ([string]::IsNullOrWhiteSpace($LauncherPath)) { return $false }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 promotes native stderr to an ErrorRecord when the caller uses Stop.
        $ErrorActionPreference = "Continue"
        & $LauncherPath -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 2)" 1>$null 2>$null
        $probeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $probeExitCode -eq 0
}

function Test-RotoWeavePython312Executable {
    param([string]$ExecutablePath)
    if ([string]::IsNullOrWhiteSpace($ExecutablePath) -or -not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        return $false
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $ExecutablePath -c "import struct, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 2)" 1>$null 2>$null
        $probeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $probeExitCode -eq 0
}

function Find-RotoWeavePython312Executable {
    $launcher = Find-RotoWeavePythonLauncher
    if ($launcher -and (Test-RotoWeavePython312Available $launcher)) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $resolved = (& $launcher -3.12 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
            $resolveExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($resolveExitCode -eq 0 -and (Test-RotoWeavePython312Executable ([string]$resolved).Trim())) {
            return [System.IO.Path]::GetFullPath(([string]$resolved).Trim())
        }
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-RotoWeavePython312Executable $candidate) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Initialize-RotoWeaveHostTools {
    param([string]$BundlePath, [switch]$AcceptHostInstall, [switch]$NonInteractive)
    $catalog = Get-RotoWeaveSourceCatalog
    $toolRoot = Join-Path $env:LOCALAPPDATA "RotoWeave\bootstrap\toolchains"
    New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null

    $pythonReady = -not [string]::IsNullOrWhiteSpace((Find-RotoWeavePython312Executable))
    if (-not $pythonReady) {
        if (-not (Confirm-RotoWeaveAction "Python 3.12 x64 is missing. Install the verified project toolchain now?" -Approved:$AcceptHostInstall -NonInteractive:$NonInteractive)) {
            throw "Python 3.12 x64 is required; installation was not authorized."
        }
        $source = $catalog.componentSources | Where-Object { $_.id -eq "python-3.12.10-amd64" } | Select-Object -First 1
        $installer = Join-Path $toolRoot ([string]$source.filename)
        if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
            if (-not (Export-RotoWeavechainFromBundle -BundlePath $BundlePath -Source $source -Destination $installer)) {
                Invoke-RotoWeaveVerifiedDownload -Source $source -Destination $installer
            }
        }
        Assert-RotoWeaveAuthenticode -Path $installer -Publisher ([string]$source.authenticodePublisher)
        $process = Start-Process -FilePath $installer -ArgumentList @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "Include_test=0") -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -ne 0) { throw "Python installer failed with exit code $($process.ExitCode)." }
        $launcherDirectory = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher"
        if (Test-Path -LiteralPath (Join-Path $launcherDirectory "py.exe") -PathType Leaf) {
            $env:Path = $launcherDirectory + ";" + $env:Path
        }
    }

    $nodeReady = $false
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($node) {
        try { $nodeReady = ([version]((& $node.Source --version).Trim().TrimStart("v"))) -ge [version]"22.13.0" } catch { $nodeReady = $false }
    }
    if (-not $nodeReady) {
        if (-not (Confirm-RotoWeaveAction "Node.js is missing or too old. Deploy the verified portable Node toolchain now?" -Approved:$AcceptHostInstall -NonInteractive:$NonInteractive)) {
            throw "Node.js 22.13.0 or newer is required; deployment was not authorized."
        }
        $source = $catalog.componentSources | Where-Object { $_.id -eq "node-24.19.0-win-x64" } | Select-Object -First 1
        $archivePath = Join-Path $toolRoot ([string]$source.filename)
        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
            if (-not (Export-RotoWeavechainFromBundle -BundlePath $BundlePath -Source $source -Destination $archivePath)) {
                Invoke-RotoWeaveVerifiedDownload -Source $source -Destination $archivePath
            }
        }
        $nodeRoot = Join-Path $toolRoot "node-24.19.0"
        if (-not (Test-Path -LiteralPath (Join-Path $nodeRoot "node-v24.19.0-win-x64\node.exe") -PathType Leaf)) {
            $stage = Join-Path $toolRoot ("node-stage-" + [guid]::NewGuid().ToString("N"))
            Expand-Archive -LiteralPath $archivePath -DestinationPath $stage
            if (Test-Path -LiteralPath $nodeRoot) { throw "Existing portable Node directory is invalid: $nodeRoot" }
            Move-Item -LiteralPath $stage -Destination $nodeRoot
        }
        $env:Path = (Join-Path $nodeRoot "node-v24.19.0-win-x64") + ";" + $env:Path
    }

    $vcInstalled = $false
    $vcKey = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
    if ($vcKey -and $vcKey.Installed -eq 1) { $vcInstalled = $true }
    if (-not $vcInstalled) {
        if (-not (Confirm-RotoWeaveAction "Microsoft Visual C++ x64 runtime is missing. Install it now?" -Approved:$AcceptHostInstall -NonInteractive:$NonInteractive)) {
            throw "Microsoft Visual C++ x64 runtime is required; installation was not authorized."
        }
        $source = $catalog.componentSources | Where-Object { $_.id -like "vc-redist-*" } | Select-Object -First 1
        $installer = Join-Path $toolRoot ([string]$source.filename)
        if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
            if (-not (Export-RotoWeavechainFromBundle -BundlePath $BundlePath -Source $source -Destination $installer)) {
                Invoke-RotoWeaveVerifiedDownload -Source $source -Destination $installer
            }
        }
        Assert-RotoWeaveAuthenticode -Path $installer -Publisher ([string]$source.authenticodePublisher)
        $process = Start-Process -FilePath $installer -ArgumentList @("/install", "/quiet", "/norestart") -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -notin @(0, 3010)) { throw "Visual C++ runtime installer failed with exit code $($process.ExitCode)." }
    }
}

function Get-RotoWeaveBootstrapPython {
    $pythonPath = Find-RotoWeavePython312Executable
    if ([string]::IsNullOrWhiteSpace($pythonPath)) {
        throw "Python 3.12 x64 was not found. Run Setup-RotoWeave.cmd to install it."
    }
    return $pythonPath
}

function Assert-RotoWeaveNode {
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $node -or -not $npm) {
        throw "Node.js/npm was not found. Install Node.js 22.13.0 or newer and reopen the terminal."
    }
    $nodePath = $node.Source
    $raw = (& $nodePath --version).Trim().TrimStart("v")
    try { $version = [version]$raw } catch { throw "Cannot parse the Node.js version: $raw" }
    if ($version -lt [version]"22.13.0") {
        throw "Node.js $version is too old; version 22.13.0 or newer is required."
    }
}

function Get-RotoWeaveBootstrapPaths {
    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    return [ordered]@{
        ProjectRoot = $projectRoot
        Bootstrap = Join-Path $projectRoot "scripts\rotoweave_bootstrap.py"
    }
}

function Test-RotoWeaveTcpPort {
    param([string]$HostName, [int]$Port, [int]$TimeoutMilliseconds = 500)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}
