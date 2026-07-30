$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$workspace = $env:GITHUB_WORKSPACE
$assetDir = Join-Path $workspace 'control\r26123'
$evidence = Join-Path $workspace 'evidence'
New-Item -ItemType Directory -Path $evidence -Force | Out-Null

$uri = 'https://api.github.com/repos/zoolove1/miru-public-browser-runner/actions/artifacts/8742968839/zip'
$artifactZip = Join-Path $workspace 'base-artifact.zip'
curl.exe -sS -L -H "Authorization: Bearer $env:GITHUB_TOKEN" -H 'Accept: application/vnd.github+json' $uri -o $artifactZip
if ($LASTEXITCODE -ne 0) { throw "artifact download failed: $LASTEXITCODE" }
$artifactDir = Join-Path $workspace 'base-artifact'
Expand-Archive -LiteralPath $artifactZip -DestinationPath $artifactDir -Force
$baseZip = Get-ChildItem -LiteralPath $artifactDir -File -Recurse |
    Where-Object { $_.Name -like '*R2.6.12.2*CHANNEL_RECOVERY*.zip' } |
    Select-Object -First 1
if ($null -eq $baseZip) { throw 'Verified R2.6.12.2 base ZIP was not found in artifact 8742968839.' }

function Join-AssetParts([string]$Filter,[int]$ExpectedCount,[string]$OutputName,[string]$ExpectedSha256) {
    $outPath = Join-Path $assetDir $OutputName
    $parts = @(Get-ChildItem -LiteralPath $assetDir -Filter $Filter -File | Sort-Object Name)
    if ($parts.Count -ne $ExpectedCount) { throw "expected $ExpectedCount chunks for $Filter, got $($parts.Count)" }
    $stream = [IO.File]::Open($outPath,[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {
        foreach ($part in $parts) {
            $bytes = [IO.File]::ReadAllBytes($part.FullName)
            $stream.Write($bytes,0,$bytes.Length)
        }
    } finally { $stream.Dispose() }
    $actual = (Get-FileHash -LiteralPath $outPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256) { throw "asset checksum mismatch for $OutputName`: $actual" }
}
Join-AssetParts -Filter 'agent.part-*' -ExpectedCount 8 -OutputName 'agent.ps1.gz.b64' -ExpectedSha256 'b1013aa2c25e5acae3b9fd1ba20dcfe245e94f976c416c79c591fb662a4af9a4'
Join-AssetParts -Filter 'test.part-*' -ExpectedCount 4 -OutputName 'test.ps1.gz.b64' -ExpectedSha256 'f3cd48bbd453bb9ff3945229db8d646c0044ad8374984c72454763e9ba042919'

python (Join-Path $assetDir 'repack_r26123.py') --base-zip $baseZip.FullName --assets $assetDir --out $evidence
if ($LASTEXITCODE -ne 0) { throw "ACK repack failed: $LASTEXITCODE" }
$candidate = Get-ChildItem -LiteralPath $evidence -File -Filter 'MIRU_PC_R2.6.12.3_ACK_SPLIT_WINDOWS_VERIFIED_CANDIDATE.zip' | Select-Object -First 1
if ($null -eq $candidate) { throw 'ACK split candidate ZIP missing.' }
python (Join-Path $assetDir 'targetize_r26123.py') --candidate $candidate.FullName --out $evidence
if ($LASTEXITCODE -ne 0) { throw "short-path ZIP build failed: $LASTEXITCODE" }
$final = Get-ChildItem -LiteralPath $evidence -File -Filter 'MIRU_R26123_INSTALLER_FIX2.zip' | Select-Object -First 1
if ($null -eq $final) { throw 'Final short-path ZIP missing.' }

# Reproduce Windows Explorer's default extraction folder (ZIP stem) exactly.
$extractContainer = Join-Path $workspace $final.BaseName
Expand-Archive -LiteralPath $final.FullName -DestinationPath $extractContainer -Force
$patchRoot = Join-Path $extractContainer 'MIRU_PC_STABILITY_PATCH_R2.6.12.3'
if (-not (Test-Path -LiteralPath $patchRoot -PathType Container)) { throw 'Patch root missing after default extraction.' }
$installerScript = Join-Path $patchRoot 'payload\scripts\Install-MiruPcStabilityPatch.ps1'
$launcherPath = Join-Path $patchRoot '01_INSTALL_MIRU_PC_STABILITY_PATCH.cmd'
if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) { throw 'Installer script missing after default extraction.' }
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) { throw 'Launcher missing after default extraction.' }
if ($installerScript.Length -ge 240) { throw "Installer path remains too long: $($installerScript.Length)" }

$tests = @(
    'payload\scripts\Test-MiruPatchStatic.ps1',
    'payload\scripts\Test-MiruAckSplit.ps1',
    'payload\scripts\Test-MiruTerminationPropagation.ps1',
    'payload\scripts\Test-MiruInstallerRollback.ps1'
)
foreach ($relative in $tests) {
    $script = Join-Path $patchRoot $relative
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -PatchRoot $patchRoot
    if ($LASTEXITCODE -ne 0) { throw "Windows validation failed: $relative exit=$LASTEXITCODE" }
}

$agentPath = Join-Path $patchRoot 'payload\root\Start-MiruSlidesControlAgent.ps1'
$installerPath = Join-Path $patchRoot 'payload\scripts\Install-MiruPcStabilityPatch.ps1'
$agent = Get-Content -Raw -LiteralPath $agentPath
$installer = Get-Content -Raw -LiteralPath $installerPath
$launcher = Get-Content -Raw -LiteralPath $launcherPath
foreach ($required in @('Get-MiruAckOutcome','Test-FrameDeliveryIsPrimaryOperation','SUCCEEDED_WITH_FRAME_ERROR','overall_status = $State','command_received = $true','operation_status = $OperationStatus','frame_status = $FrameStatus','frame_error = $FrameError','COMMAND_SUCCEEDED_WITH_FRAME_ERROR')) {
    if (-not $agent.Contains($required)) { throw "ACK contract missing: $required" }
}
if (-not $agent.Contains('$AgentVersion = ''0.9.9.2''')) { throw 'Control agent version is not 0.9.9.2.' }
foreach ($required in @('agentSource','agentTarget','agentTemp','control_agent_installed = $true','control_agent_version = ''0.9.9.2''','ack_operation_frame_split = $true')) {
    if (-not $installer.Contains($required)) { throw "Transactional installer contract missing: $required" }
}
$target = 'C:\Users\sikchung\Downloads\MIRU_PC_COMPLETE\MIRU_PC_COMPLETE_V0970_CLEAN1'
if (-not $launcher.Contains($target)) { throw 'Final installer is not targeted to the expected MIRU PC root.' }
if ($launcher.Contains('-PatchRoot')) { throw 'Unsupported -PatchRoot remains in launcher.' }
if (([regex]::Matches($launcher,'-TargetRoot')).Count -ne 1) { throw 'Launcher must pass -TargetRoot exactly once.' }
$tokens = $null; $errors = $null
[void][Management.Automation.Language.Parser]::ParseFile($agentPath,[ref]$tokens,[ref]$errors)
if (@($errors).Count -ne 0) { throw ('Agent parser errors: ' + (($errors | ForEach-Object Message) -join '; ')) }
$sha = (Get-FileHash -LiteralPath $final.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$shaFile = "$($final.FullName).sha256"
$recorded = ((Get-Content -Raw -LiteralPath $shaFile) -split '\s+')[0].ToLowerInvariant()
if ($sha -ne $recorded) { throw "Final ZIP checksum mismatch: $sha vs $recorded" }
@(
    'MIRU_PC_R26123_SHORT_PATH_WINDOWS_E2E=PASS',
    "FINAL_SHA256=$sha",
    "DEFAULT_EXTRACTION_INSTALLER_PATH_LENGTH=$($installerScript.Length)",
    'DEFAULT_EXTRACTION_INSTALLER_EXISTS=PASS',
    'ACK_INPUT_FRAME_FAILURE=SUCCEEDED_WITH_FRAME_ERROR',
    'ACK_FRAME_ONLY_FAILURE=FAILED',
    'TRANSACTIONAL_AGENT_ROLLBACK=PASS',
    'TARGET_ROOT_SINGLE_BINDING=PASS',
    'PATCH_ROOT_ARGUMENT_ABSENT=PASS'
) | Set-Content -LiteralPath (Join-Path $evidence 'VALIDATION_REPORT_R26123_SHORT_PATH.txt') -Encoding ascii
Write-Host 'MIRU_PC_R26123_SHORT_PATH_WINDOWS_E2E=PASS'
