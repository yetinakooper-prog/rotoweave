param(
    [ValidateSet("Client", "Server", "All")]
    [string]$Role = "Client",
    [string]$BundlePath,
    [string]$BundleDirectory,
    [string]$BundleSource,
    [string]$ExpectedBundleSha256,
    [switch]$NonInteractive,
    [switch]$AcceptDownload,
    [switch]$AcceptHostInstall,
    [switch]$ConfigureFirewall,
    [switch]$Repair
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Bootstrap-Common.ps1")
$paths = Get-RotoWeaveBootstrapPaths
$normalizedRole = Resolve-RotoWeaveRole $Role
$nativeArchitecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($nativeArchitecture -ne "AMD64") { throw "RotoWeave Setup currently supports Windows x64 only; detected $nativeArchitecture." }

function Resolve-RotoWeaveBundleFromUserInput {
    param([string]$InputPath)
    if ([string]::IsNullOrWhiteSpace($InputPath)) { return $null }
    $candidate = if ([System.IO.Path]::IsPathRooted($InputPath)) {
        $InputPath
    } else {
        [System.IO.Path]::Combine((Get-Location).Path, $InputPath)
    }
    $resolved = [System.IO.Path]::GetFullPath($candidate)
    if (Test-Path -LiteralPath $resolved -PathType Leaf) {
        if ([System.IO.Path]::GetExtension($resolved).ToLowerInvariant() -ne ".zip") { throw "Deployment bundle must be a .zip file." }
        return $resolved
    }
    if (Test-Path -LiteralPath $resolved -PathType Container) { return $resolved }
    throw "Deployment bundle path does not exist: $resolved"
}

function Read-RotoWeaveBootstrapBundleManifest {
    param([string]$BundlePath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead([System.IO.Path]::GetFullPath($BundlePath))
    try {
        $manifestNames = @("RotoWeave-DEPLOYMENT.json", "AIFrameTools-DEPLOYMENT.json")
        $entries = @($archive.Entries | Where-Object { $manifestNames -contains $_.FullName.Replace("\", "/") })
        if ($entries.Count -ne 1 -or $entries[0].Length -gt 1MB) { return $null }
        $stream = $entries[0].Open()
        try {
            $reader = [System.IO.StreamReader]::new($stream, [System.Text.UTF8Encoding]::new($false, $true), $true)
            try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
        } finally { $stream.Dispose() }
    } finally { $archive.Dispose() }
}

function Find-RotoWeaveHostBootstrapBundle {
    param([string]$ResolvedInput, [string]$ExpectedRole, [string]$ProjectRoot)
    if ([string]::IsNullOrWhiteSpace($ResolvedInput)) { return $null }
    if (Test-Path -LiteralPath $ResolvedInput -PathType Leaf) { return $ResolvedInput }
    if (-not (Test-Path -LiteralPath $ResolvedInput -PathType Container)) { return $null }

    $product = Get-Content -LiteralPath (Join-Path $ProjectRoot "RotoWeaveContracts\product.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $protocol = Get-Content -LiteralPath (Join-Path $ProjectRoot "RotoWeaveContracts\deployment-protocol.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $compatible = @(
        Get-ChildItem -LiteralPath $ResolvedInput -File -Filter "*.zip" | ForEach-Object {
            try {
                $manifest = Read-RotoWeaveBootstrapBundleManifest $_.FullName
                if ($manifest -and
                    $manifest.schemaVersion -eq $protocol.schemaVersion -and
                    $manifest.productVersion -eq $product.version -and
                    $manifest.platform -eq $protocol.platform -and
                    $manifest.role -eq $ExpectedRole) {
                    $_.FullName
                }
            } catch {
                # Full bundle compatibility and hashes are verified by the Python importer after bootstrapping.
            }
        }
    )
    if ($compatible.Count -gt 1) { throw "目录中存在多个可用于启动的 $ExpectedRole 部署 ZIP；请改为输入准确 ZIP 文件。" }
    if ($compatible.Count -eq 1) { return $compatible[0] }
    return $null
}

function Ask-RotoWeaveExistingBundle {
    param([switch]$DownloadAvailable)
    if ($NonInteractive) { return $null }
    $question = if ($DownloadAvailable) {
        "是否已有其他设备导出的 RotoWeave 部署 ZIP？如有可优先避免下载 [Y/n]"
    } else {
        "当前未配置可下载的完整部署包。是否已有其他设备导出的 RotoWeave 部署 ZIP？ [Y/n]"
    }
    $answer = Read-Host $question
    if (-not [string]::IsNullOrWhiteSpace($answer) -and $answer.Trim().ToLowerInvariant() -notin @("y", "yes", "是")) { return $null }
    $inputPath = Read-Host "请输入 ZIP 文件或包含 ZIP 的目录"
    return Resolve-RotoWeaveBundleFromUserInput $inputPath
}

function Get-RotoWeaveServerHostStatus {
    param([string]$NvidiaSmiPath)

    if ([string]::IsNullOrWhiteSpace($NvidiaSmiPath)) {
        $nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if (-not $nvidia) {
            return [ordered]@{
                installable=$true; ready=$false; profileCandidate=$false
                detail="未检测到 nvidia-smi；安装与服务 API 启动不受阻。"; command=$null; devices=@()
                warnings=@([ordered]@{code="gpu_not_detected";severity="warning";scope="host";message="未检测到 NVIDIA 设备工具。";action="安装兼容 NVIDIA 驱动后重新执行 Profile 自检。"})
            }
        }
        $NvidiaSmiPath = $nvidia.Source
    }

    # Capturing a native command through a PowerShell pipeline can leave
    # $LASTEXITCODE unset in PowerShell 7. Freeze output first, then read the
    # native exit code before running any other command.
    $gpuOutput = @(& $NvidiaSmiPath --query-gpu=index,uuid,name,driver_version,compute_cap,memory.total,memory.used --format=csv,noheader,nounits 2>&1)
    $gpuQueryExitCode = $LASTEXITCODE
    if ($gpuQueryExitCode -ne 0) {
        return [ordered]@{
            installable=$true; ready=$false; profileCandidate=$false
            detail="nvidia-smi GPU 查询失败 (exit=$gpuQueryExitCode)；安装与服务 API 启动不受阻。"; command=$NvidiaSmiPath; devices=@()
            warnings=@([ordered]@{code="nvidia_smi_failed";severity="warning";scope="host";message="NVIDIA 设备查询失败。";action="检查驱动后重新执行 Profile 自检。"})
        }
    }
    $devices = @()
    foreach ($line in $gpuOutput) {
        $values = @(([string]$line).Split(',') | ForEach-Object { $_.Trim() })
        if ($values.Count -ne 7) { continue }
        $devices += [ordered]@{
            index=[int]$values[0]; uuid=$values[1]; name=$values[2]; driverVersion=$values[3]
            computeCapability=$values[4]; vramTotalMiB=[int]$values[5]; vramUsedMiB=[int]$values[6]
            vramFreeMiB=([int]$values[5]-[int]$values[6])
        }
    }
    $devices = @($devices | Sort-Object @{Expression="vramTotalMiB";Descending=$true}, @{Expression="index";Descending=$false})
    if ($devices.Count -eq 0) {
        return [ordered]@{
            installable=$true; ready=$false; profileCandidate=$false
            detail="未枚举到可识别的 NVIDIA GPU；安装与服务 API 启动不受阻。"; command=$NvidiaSmiPath; devices=@()
            warnings=@([ordered]@{code="gpu_not_detected";severity="warning";scope="host";message="未枚举到可识别的 NVIDIA GPU。";action="检查驱动和设备状态后重新执行 Profile 自检。"})
        }
    }

    $smiOutput = @(& $NvidiaSmiPath 2>&1)
    $smiExitCode = $LASTEXITCODE
    if ($smiExitCode -ne 0) {
        return [ordered]@{
            installable=$true; ready=$false; profileCandidate=$false
            detail="nvidia-smi 驱动状态查询失败 (exit=$smiExitCode)；安装与服务 API 启动不受阻。"; command=$NvidiaSmiPath; devices=$devices
            warnings=@([ordered]@{code="driver_incompatible";severity="warning";scope="host";message="NVIDIA 驱动状态查询失败。";action="更新驱动后重新执行 Profile 自检。"})
        }
    }
    $smiText = $smiOutput -join "`n"
    $match = [regex]::Match($smiText, "CUDA Version:\s*(\d+)\.(\d+)")
    if (-not $match.Success -or [version]("$($match.Groups[1].Value).$($match.Groups[2].Value)") -lt [version]"12.8") {
        $reported = if ($match.Success) { $match.Value } else { "未知 CUDA 兼容级别" }
        return [ordered]@{
            installable=$true; ready=$false; profileCandidate=$false
            detail="NVIDIA 驱动不满足 CUDA 12.8 兼容要求: $reported；安装与服务 API 启动不受阻。"; command=$NvidiaSmiPath; devices=$devices
            warnings=@([ordered]@{code="driver_incompatible";severity="warning";scope="host";message="NVIDIA 驱动不满足固定 CUDA 12.8 运行时要求。";action="更新驱动后重新执行 Profile 自检。"})
        }
    }
    $selected = $devices[0]
    return [ordered]@{
        installable=$true; ready=$true; profileCandidate=$true
        detail="$($selected.name) / $($match.Value)"; command=$NvidiaSmiPath
        selectedDevice=$selected; devices=$devices; warnings=@()
    }
}

$resolvedInput = $null
if (-not [string]::IsNullOrWhiteSpace($BundlePath)) { $resolvedInput = Resolve-RotoWeaveBundleFromUserInput $BundlePath }
elseif (-not [string]::IsNullOrWhiteSpace($BundleDirectory)) { $resolvedInput = Resolve-RotoWeaveBundleFromUserInput $BundleDirectory }

$hostBundle = Find-RotoWeaveHostBootstrapBundle -ResolvedInput $resolvedInput -ExpectedRole $normalizedRole -ProjectRoot $paths.ProjectRoot
Initialize-RotoWeaveHostTools -BundlePath $hostBundle -AcceptHostInstall:$AcceptHostInstall -NonInteractive:$NonInteractive
$pythonLauncher = Get-RotoWeaveBootstrapPython
Assert-RotoWeaveNode

if ($resolvedInput -and (Test-Path -LiteralPath $resolvedInput -PathType Container)) {
    $selectionJson = & $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot select-bundle --role $normalizedRole --directory $resolvedInput --json
    if ($LASTEXITCODE -ne 0) { throw "No unique compatible deployment ZIP was found in the selected directory." }
    $resolvedBundle = ($selectionJson | ConvertFrom-Json).bundlePath
} else { $resolvedBundle = $resolvedInput }

$preflightJson = & $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot check --role $normalizedRole --full-hash --json
$preflight = $preflightJson | ConvertFrom-Json
$basicBuildResult = $null
$serverRuntimeBuildResult = $null
if ($normalizedRole -in @("client", "all")) {
    $basicCheck = $preflight.checks | Where-Object { $_.key -eq "client-basic" } | Select-Object -First 1
    if (-not $basicCheck -or $basicCheck.status -ne "ready") {
        if (-not (Confirm-RotoWeaveAction "Basic 模型未就绪。是否下载固定源码和导出依赖，并在本机生成 ONNX？" -Approved:$AcceptDownload -NonInteractive:$NonInteractive)) {
            throw "Basic source build requires download authorization. Re-run with -AcceptDownload or approve the interactive prompt."
        }
        Write-Host "[1/6] Building Basic ONNX from pinned source and running structural/numerical self-tests..."
        $basicArguments = @($paths.Bootstrap, "--project-root", $paths.ProjectRoot, "build-basic", "--json")
        if ($Repair) { $basicArguments += "--replace-invalid" }
        $basicBuildJson = & $pythonLauncher @basicArguments
        if ($LASTEXITCODE -ne 0) { throw "Basic ONNX source build failed. Existing valid assets were not replaced." }
        $basicBuildResult = $basicBuildJson | ConvertFrom-Json
        $preflightJson = & $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot check --role $normalizedRole --full-hash --json
        $preflight = $preflightJson | ConvertFrom-Json
        $basicCheck = $preflight.checks | Where-Object { $_.key -eq "client-basic" } | Select-Object -First 1
        if (-not $basicCheck -or $basicCheck.status -ne "ready") { throw "Basic source build completed but the product readiness check still failed." }
    }
}
if ($normalizedRole -in @("server", "all") -and [string]::IsNullOrWhiteSpace($resolvedBundle) -and [string]::IsNullOrWhiteSpace($BundleSource)) {
    $runtimeCheck = $preflight.checks | Where-Object { $_.key -eq "server-runtimes" } | Select-Object -First 1
    if (-not $runtimeCheck -or $runtimeCheck.status -ne "ready") {
        $runtimePrompt = if ($runtimeCheck -and $runtimeCheck.status -eq "invalid") {
            "Server 固定运行时与当前契约不一致。是否下载官方 Python、固定依赖和源码，在 staging 中重建并备份替换旧运行时？"
        } else {
            "Server 固定运行时未就绪。是否下载官方 Python、固定依赖和源码，在本机动态生成 High/Ultra 运行时？"
        }
        if (-not (Confirm-RotoWeaveAction $runtimePrompt -Approved:$AcceptDownload -NonInteractive:$NonInteractive)) {
            throw "Server runtime source build requires download authorization. Re-run with -AcceptDownload or approve the interactive prompt."
        }
        Write-Host "[2/6] 正在下载、校验并生成 Server High/Ultra 固定运行时；下方会持续显示文件进度、速度和预计剩余时间..."
        $runtimeArguments = @($paths.Bootstrap, "--project-root", $paths.ProjectRoot, "build-server-runtimes", "--progress", "--json")
        if (($runtimeCheck -and $runtimeCheck.status -eq "invalid") -or $Repair) { $runtimeArguments += "--replace-invalid" }
        $runtimeBuildJson = & $pythonLauncher @runtimeArguments
        if ($LASTEXITCODE -ne 0) { throw "Server runtime source build failed. Existing runtime data was not replaced unless staging validation completed." }
        $serverRuntimeBuildResult = $runtimeBuildJson | ConvertFrom-Json
        $preflightJson = & $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot check --role $normalizedRole --full-hash --json
        $preflight = $preflightJson | ConvertFrom-Json
        $runtimeCheck = $preflight.checks | Where-Object { $_.key -eq "server-runtimes" } | Select-Object -First 1
        if (-not $runtimeCheck -or $runtimeCheck.status -ne "ready") { throw "Server runtime source build completed but the product readiness check still failed." }
    }
}
$needsRecovery = -not $preflight.ready
$plan = (& $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot plan --role $normalizedRole --json) | ConvertFrom-Json
$needsBundleRecovery = @($plan.components | Where-Object { $_.status -ne "ready" }).Count -gt 0
$hasEnvironmentGap = @($preflight.checks | Where-Object { $_.key -like "*-environment" -and $_.status -ne "ready" }).Count -gt 0
$catalog = Get-RotoWeaveSourceCatalog
if ([string]::IsNullOrWhiteSpace($BundleSource)) {
    $matchingBundleSources = @($catalog.bundleSources | Where-Object {
        ($_.role -eq $normalizedRole -or $_.roles -contains $normalizedRole) -and
        $_.platform -eq $plan.platform -and
        $_.productVersion -eq $plan.productVersion -and
        $_.compatibilityDigest -eq $plan.compatibilityDigest
    })
    if ($matchingBundleSources.Count -gt 1) { throw "受控源清单存在多个兼容部署 ZIP；请使用 -BundleSource 精确指定。" }
    if ($matchingBundleSources.Count -eq 1) {
        $BundleSource = [string]$matchingBundleSources[0].url
        $ExpectedBundleSha256 = [string]$matchingBundleSources[0].sha256
    }
}
$receiptRoot = Join-Path $env:LOCALAPPDATA "RotoWeave\deployment-receipts"
$receiptPath = Join-Path $receiptRoot ("$normalizedRole.json")
$existingReceipt = $null
if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
    try { $existingReceipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $existingReceipt = $null }
}
if (-not $needsRecovery -and $existingReceipt -and $existingReceipt.role -eq $normalizedRole -and $existingReceipt.compatibilityDigest -eq $plan.compatibilityDigest) {
    Write-Host "Existing deployment receipt and all readiness checks are valid; no redeployment is required." -ForegroundColor Green
    Write-Host "Next: Start-RotoWeave.cmd $Role" -ForegroundColor Green
    exit 0
}

if ($needsBundleRecovery -and [string]::IsNullOrWhiteSpace($resolvedBundle)) {
    $candidate = Ask-RotoWeaveExistingBundle -DownloadAvailable:(-not [string]::IsNullOrWhiteSpace($BundleSource))
    if ($candidate) {
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $selectionJson = & $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot select-bundle --role $normalizedRole --directory $candidate --json
            if ($LASTEXITCODE -ne 0) { throw "No unique compatible deployment ZIP was found in the selected directory." }
            $resolvedBundle = ($selectionJson | ConvertFrom-Json).bundlePath
        } else { $resolvedBundle = $candidate }
    }
}

$downloadedBundle = $false
if ($needsBundleRecovery -and [string]::IsNullOrWhiteSpace($resolvedBundle) -and -not [string]::IsNullOrWhiteSpace($BundleSource)) {
    if ([string]::IsNullOrWhiteSpace($ExpectedBundleSha256)) { throw "-BundleSource requires -ExpectedBundleSha256." }
    if (-not (Confirm-RotoWeaveAction "未找到本地 ZIP。是否从受控来源下载部署包？" -Approved:$AcceptDownload -NonInteractive:$NonInteractive)) { throw "Deployment bundle download was not authorized." }
    $downloadRoot = Join-Path $env:LOCALAPPDATA "RotoWeave\downloads"
    New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
    $resolvedBundle = Join-Path $downloadRoot ("RotoWeave-" + $normalizedRole + "-" + $ExpectedBundleSha256.Substring(0, 12) + ".zip")
    & $pythonLauncher $paths.Bootstrap --project-root $paths.ProjectRoot download-bundle --source $BundleSource --output $resolvedBundle --expected-sha256 $ExpectedBundleSha256 --json
    if ($LASTEXITCODE -ne 0) { throw "Controlled deployment ZIP download failed." }
    $downloadedBundle = $true
}

$offlinePayloadRoot = $null
if ($needsBundleRecovery) {
    if ([string]::IsNullOrWhiteSpace($resolvedBundle)) {
        $missing = @($plan.components | Where-Object { $_.status -ne "ready" } | ForEach-Object { $_.id }) -join ", "
        if ([string]::IsNullOrWhiteSpace($BundleSource)) {
            throw "当前 Server 资产没有可下载的完整部署包，且未选择本地 ZIP。请从已部署 Server 导出 ZIP，或由发布管理员提供 -BundleSource 与 -ExpectedBundleSha256。当前缺失：$missing。Client Basic 不使用 ZIP，并已独立从固定源码生成。"
        }
        throw "未找到可用的部署输入：$missing。请提供 -BundlePath/-BundleDirectory，或同时提供 -BundleSource 与 -ExpectedBundleSha256。"
    }
}
$shouldImportBundle = -not [string]::IsNullOrWhiteSpace($resolvedBundle) -and ($needsBundleRecovery -or $hasEnvironmentGap -or $normalizedRole -eq "client")
if ($shouldImportBundle) {
    Write-Host "[2/6] Verifying and importing schema 2 deployment ZIP environment/Server payload..."
    $importArguments = @($paths.Bootstrap, "--project-root", $paths.ProjectRoot, "import-bundle", "--role", $normalizedRole, "--bundle", [System.IO.Path]::GetFullPath($resolvedBundle), "--json")
    if (-not [string]::IsNullOrWhiteSpace($ExpectedBundleSha256)) { $importArguments += @("--expected-sha256", $ExpectedBundleSha256) }
    if ($Repair) { $importArguments += "--replace-invalid" }
    $importJson = & $pythonLauncher @importArguments
    if ($LASTEXITCODE -ne 0) { throw "Deployment ZIP verification/import failed." }
    $offlinePayloadRoot = ($importJson | ConvertFrom-Json).payloadRoot
}

if ($normalizedRole -in @("client", "all")) {
    Write-Host "[3/6] Rebuilding RotoWeaveClient environment..."
    & (Join-Path $paths.ProjectRoot "RotoWeaveClient\Setup.ps1") -EnvironmentOnly -OfflinePayloadRoot $offlinePayloadRoot
}
if ($normalizedRole -in @("server", "all")) {
    Write-Host "[4/6] Rebuilding RotoWeaveServer environment and fixed runtimes..."
    & (Join-Path $paths.ProjectRoot "RotoWeaveServer\Setup.ps1") -EnvironmentOnly -OfflinePayloadRoot $offlinePayloadRoot
    $serverHost = Get-RotoWeaveServerHostStatus
    foreach ($warning in @($serverHost.warnings)) {
        Write-Warning ("[{0}] {1} {2}" -f $warning.code, $warning.message, $warning.action)
    }
    $modelPreparation = "user-managed-after-setup"
    if (-not $serverHost.ready) {
        $catalog = Get-RotoWeaveSourceCatalog
        $driver = $catalog.guidedHostSources | Where-Object { $_.id -eq "nvidia-cuda-12.8-capable-driver" } | Select-Object -First 1
        if (Confirm-RotoWeaveAction "$($serverHost.detail)。是否打开 NVIDIA 官方驱动入口？" -Approved:$AcceptHostInstall -NonInteractive:$NonInteractive) {
            Start-Process -FilePath ([string]$driver.url)
        }
    } else {
        Write-Host "Server CUDA candidate: $($serverHost.detail)"
    }
    Write-Warning "服务端环境和 API 已就绪；Setup 不扫描、下载、绑定或激活模型。请由用户在模型中心完成五个模型的绑定、校验、自检和激活。"
    $shouldConfigureFirewall = $ConfigureFirewall -or (Confirm-RotoWeaveAction "是否为 Private 网络添加服务端 LAN API 防火墙规则？" -NonInteractive:$NonInteractive)
    if ($shouldConfigureFirewall) {
        $ruleName = "RotoWeave Server LAN API"
        $command = "if(-not (Get-NetFirewallRule -DisplayName '$ruleName' -ErrorAction SilentlyContinue)){New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8443 -Profile Private -RemoteAddress LocalSubnet | Out-Null}"
        $firewallProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-NonInteractive", "-Command", $command) -Verb RunAs -Wait -PassThru -WindowStyle Hidden
        if ($firewallProcess.ExitCode -ne 0) { throw "Windows Firewall rule creation failed." }
    }
}

Write-Host "[6/6] Running final readiness check..."
$checkArguments = @($paths.Bootstrap, "--project-root", $paths.ProjectRoot, "check", "--role", $normalizedRole, "--full-hash")
& $pythonLauncher @checkArguments
if ($LASTEXITCODE -ne 0) { throw "Setup completed, but the final readiness check failed." }

New-Item -ItemType Directory -Force -Path $receiptRoot | Out-Null
$receiptBundlePath = if ($downloadedBundle) { $null } else { $resolvedBundle }
$receiptHardwareWarnings = [System.Collections.Generic.List[object]]::new()
if ($serverHost) {
    foreach ($warning in @($serverHost.warnings)) {
        $receiptHardwareWarnings.Add($warning)
    }
}
$receiptRecommendedActions = [System.Collections.Generic.List[string]]::new()
if ($normalizedRole -in @("server", "all")) {
    $receiptRecommendedActions.Add("自主取得五个模型文件，并在模型中心绑定、校验、自检和激活。")
    foreach ($warning in @($serverHost.warnings)) {
        $receiptRecommendedActions.Add([string]$warning.action)
    }
}
$receipt = [ordered]@{
    schemaVersion = 2
    role = $normalizedRole
    productVersion = $plan.productVersion
    compatibilityDigest = $plan.compatibilityDigest
    completedAtUtc = [DateTime]::UtcNow.ToString("o")
    bundlePath = $receiptBundlePath
    basicSourceBuild = $basicBuildResult
    serverRuntimeSourceBuild = $serverRuntimeBuildResult
    installSucceeded = $true
    hardwareWarnings = $receiptHardwareWarnings.ToArray()
    modelPreparation = $modelPreparation
    profileStates = if ($normalizedRole -in @("server", "all")) { [ordered]@{high="user-managed";ultra="user-managed"} } else { [ordered]@{} }
    recommendedActions = $receiptRecommendedActions.ToArray()
}
$receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
$pendingReceipt = Join-Path $receiptRoot ("$normalizedRole-pending.json")
if (Test-Path -LiteralPath $pendingReceipt -PathType Leaf) { Remove-Item -LiteralPath $pendingReceipt -Force }
if ($downloadedBundle -and (Test-Path -LiteralPath $resolvedBundle -PathType Leaf)) { Remove-Item -LiteralPath $resolvedBundle -Force }

Write-Host ""
Write-Host "Setup succeeded. Next: Start-RotoWeave.cmd $Role" -ForegroundColor Green
