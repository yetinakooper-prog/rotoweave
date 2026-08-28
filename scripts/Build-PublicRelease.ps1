param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$product = Get-Content -LiteralPath (Join-Path $projectRoot "RotoWeaveContracts\product.json") -Raw | ConvertFrom-Json
$version = [string]$product.version
$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("RotoWeave-PublicRelease-" + [Guid]::NewGuid().ToString("N"))
$temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\") + "\"

if (-not ([System.IO.Path]::GetFullPath($stagingRoot) + "\").StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected temporary staging path: $stagingRoot"
}

if ($outputRoot.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Public Release output directory must be outside the source checkout."
}

$forbiddenExtensions = @(".md", ".onnx", ".pt", ".pth", ".safetensors", ".ckpt", ".pdf", ".pem", ".key", ".pfx", ".p12", ".dll", ".exe", ".whl")
$forbiddenNames = @("AGENTS.md", "THIRD_PARTY_NOTICES.md")
$forbiddenDirectoryNames = @(".git", ".svn", ".agents", ".openai", "AgentRules", "docs", "Docs", "Temp", "RotoWeaveModels", "server-runtimes", "remote-service-data", "release", "dist", ".venv", "node_modules", "licenses", "__pycache__", ".pytest_cache", "tests")
$forbiddenAbsoluteDirectories = @(
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot "RotoWeaveClient\runtime"))
)
$rootLaunchers = @("Check-RotoWeave.cmd", "Export-RotoWeaveArtifacts.cmd", "Setup-RotoWeave.cmd", "Start-RotoWeave.cmd", "Stop-RotoWeave.cmd")

function Copy-PublicTree {
    param([string]$Source, [string]$Destination)
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if (
                $forbiddenDirectoryNames -notcontains $_.Name -and
                $forbiddenAbsoluteDirectories -notcontains [System.IO.Path]::GetFullPath($_.FullName)
            ) {
                Copy-PublicTree -Source $_.FullName -Destination (Join-Path $Destination $_.Name)
            }
        }
        elseif (
            $forbiddenNames -notcontains $_.Name -and
            $_.Name -notmatch '^(?i:README|LICENSE|NOTICE|COPYING)(\.|$)' -and
            $forbiddenExtensions -notcontains $_.Extension.ToLowerInvariant()
        ) {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name)
        }
    }
}

function New-RolePackage {
    param([ValidateSet("Client", "Server")][string]$Role)
    $stage = Join-Path $stagingRoot $Role
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    foreach ($launcher in $rootLaunchers) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $launcher) -Destination (Join-Path $stage $launcher)
    }
    Copy-PublicTree -Source (Join-Path $projectRoot "scripts") -Destination (Join-Path $stage "scripts")
    Copy-PublicTree -Source (Join-Path $projectRoot "RotoWeaveContracts") -Destination (Join-Path $stage "RotoWeaveContracts")
    Copy-PublicTree -Source (Join-Path $projectRoot ("RotoWeave" + $Role)) -Destination (Join-Path $stage ("RotoWeave" + $Role))
    $archive = Join-Path $outputRoot ("RotoWeave-{0}-{1}-Windows-x64.zip" -f $Role, $version)
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archive -CompressionLevel Optimal
    return $archive
}

try {
    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
    $client = New-RolePackage -Role Client
    $server = New-RolePackage -Role Server

    & py -3.12 (Join-Path $projectRoot "RotoWeaveClient\scripts\build-unitypackage.py")
    if ($LASTEXITCODE -ne 0) { throw "Unity package build failed." }
    $unitySource = Join-Path $projectRoot "RotoWeaveClient\release\RotoWeave-UnityImporter.unitypackage"
    $unity = Join-Path $outputRoot ("RotoWeave-UnityImporter-{0}.unitypackage" -f $version)
    Copy-Item -LiteralPath $unitySource -Destination $unity

    $assets = @($client, $server, $unity)
    foreach ($asset in $assets) {
        if ((Get-Item -LiteralPath $asset).Length -ge 2GB) { throw "Release asset exceeds 2 GiB: $asset" }
    }
    $hashLines = foreach ($asset in $assets) {
        $hash = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $([System.IO.Path]::GetFileName($asset))"
    }
    [System.IO.File]::WriteAllLines((Join-Path $outputRoot "SHA256SUMS.txt"), $hashLines, [System.Text.UTF8Encoding]::new($false))
    Get-ChildItem -LiteralPath $outputRoot -File | Select-Object Name, Length
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
