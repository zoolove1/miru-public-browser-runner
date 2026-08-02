[CmdletBinding()]
param(
    [string]$Repository = 'zoolove1/useful-orbit-automation',
    [string]$RunnerName = 'MIRU-PC-SIKCHUNG',
    [string]$Labels = 'miru-pc,miru-pc-gui',
    [string]$RunnerRoot = (Join-Path $env:LOCALAPPDATA 'MIRU\actions-runner'),
    [int]$ControlIssue = 290
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$stateDir = Join-Path $env:LOCALAPPDATA 'MIRU\runner-control'
$statusPath = Join-Path $stateDir 'bootstrap-status.json'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

function Write-JsonAtomic {
    param([Parameter(Mandatory)][string]$Path,[Parameter(Mandatory)]$Value)
    $tmp = $Path + '.tmp.' + [guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText($tmp,($Value | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Invoke-GhJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $text = & gh @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ('gh failed (' + $exitCode + '): ' + (($text | Out-String).Trim()))
    }
    $raw = ($text | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    return ($raw | ConvertFrom-Json)
}

function Post-BootstrapStatus {
    param([Parameter(Mandatory)]$Payload)
    $temp = Join-Path $stateDir ('bootstrap-comment-' + [guid]::NewGuid().ToString('N') + '.txt')
    try {
        $body = "MIRU_RUNNER_BOOTSTRAP:`n" + ($Payload | ConvertTo-Json -Depth 12 -Compress)
        [IO.File]::WriteAllText($temp,$body,[Text.UTF8Encoding]::new($false))
        & gh issue comment $ControlIssue --repo $Repository --body-file $temp | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to post bootstrap status.' }
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

function Get-RunnerApiState {
    $list = Invoke-GhJson @('api',("repos/{0}/actions/runners?per_page=100" -f $Repository))
    foreach ($runner in @($list.runners)) {
        if ([string]$runner.name -eq $RunnerName) { return $runner }
    }
    return $null
}

$startedAt = (Get-Date).ToString('o')
try {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh.exe was not found in PATH.' }
    & gh auth status --hostname github.com *> $null
    if ($LASTEXITCODE -ne 0) { throw 'gh is not authenticated for github.com.' }

    New-Item -ItemType Directory -Path $RunnerRoot -Force | Out-Null

    $applications = @(Invoke-GhJson @('api',("repos/{0}/actions/runners/downloads" -f $Repository)))
    $application = $applications |
        Where-Object { ([string]$_.os).ToLowerInvariant() -eq 'win' -and ([string]$_.architecture).ToLowerInvariant() -eq 'x64' } |
        Select-Object -First 1
    if ($null -eq $application) { throw 'Windows x64 runner download was not returned by GitHub.' }

    $zipPath = Join-Path $stateDir ([string]$application.filename)
    Invoke-WebRequest -UseBasicParsing -Uri ([string]$application.download_url) -OutFile $zipPath

    $expectedHash = ([string]$application.sha256_checksum).Trim()
    if ($expectedHash.StartsWith('sha256:',[StringComparison]::OrdinalIgnoreCase)) {
        $expectedHash = $expectedHash.Substring(7)
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $actualHash -ne $expectedHash.ToLowerInvariant()) {
        throw 'Runner package SHA-256 mismatch.'
    }

    Get-CimInstance Win32_Process -Filter "Name='Runner.Listener.exe'" -ErrorAction SilentlyContinue |
        Where-Object { ([string]$_.ExecutablePath).StartsWith($RunnerRoot,[StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id ([int]$_.ProcessId) -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1

    Expand-Archive -LiteralPath $zipPath -DestinationPath $RunnerRoot -Force
    $configCmd = Join-Path $RunnerRoot 'config.cmd'
    $runCmd = Join-Path $RunnerRoot 'run.cmd'
    if (-not (Test-Path -LiteralPath $configCmd) -or -not (Test-Path -LiteralPath $runCmd)) {
        throw 'Runner package extraction did not produce config.cmd and run.cmd.'
    }

    $remoteRunner = Get-RunnerApiState
    $localConfigured = Test-Path -LiteralPath (Join-Path $RunnerRoot '.runner')

    if (-not ($localConfigured -and $null -ne $remoteRunner)) {
        if ($localConfigured) {
            $removeToken = Invoke-GhJson @('api','--method','POST',("repos/{0}/actions/runners/remove-token" -f $Repository))
            & $configCmd remove --unattended --token ([string]$removeToken.token) *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Existing local runner configuration could not be removed.' }
        }

        $registration = Invoke-GhJson @('api','--method','POST',("repos/{0}/actions/runners/registration-token" -f $Repository))
        & $configCmd `
            --unattended `
            --url ("https://github.com/{0}" -f $Repository) `
            --token ([string]$registration.token) `
            --name $RunnerName `
            --labels $Labels `
            --work '_work' `
            --replace *> $null
        if ($LASTEXITCODE -ne 0) { throw 'config.cmd failed to register the runner.' }
    }

    $startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
    $startupVbs = Join-Path $startup 'MIRU_PC_SELF_HOSTED_RUNNER.vbs'
    $escapedRun = $runCmd.Replace('"','""')
    $vbs = @"
Set shell = CreateObject("WScript.Shell")
shell.Run Chr(34) & "$escapedRun" & Chr(34), 0, False
"@
    [IO.File]::WriteAllText($startupVbs,$vbs,[Text.UTF8Encoding]::new($false))

    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $startupVbs + '"') -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(120)
    $online = $null
    do {
        Start-Sleep -Seconds 3
        $online = Get-RunnerApiState
    } while (($null -eq $online -or [string]$online.status -ne 'online') -and (Get-Date) -lt $deadline)

    if ($null -eq $online -or [string]$online.status -ne 'online') {
        throw 'Runner was registered but did not become online within 120 seconds.'
    }

    $payload = [ordered]@{
        state = 'RUNNER_ONLINE'
        repository = $Repository
        runner_name = $RunnerName
        runner_root = $RunnerRoot
        status = [string]$online.status
        busy = [bool]$online.busy
        labels = @($online.labels | ForEach-Object { [string]$_.name })
        package_file = [string]$application.filename
        package_sha256 = $actualHash
        startup_mode = 'LOGGED_IN_USER_STARTUP'
        service_session0 = $false
        started_at = $startedAt
        finished_at = (Get-Date).ToString('o')
    }
    Write-JsonAtomic -Path $statusPath -Value $payload
    Post-BootstrapStatus -Payload $payload
    Write-Host 'MIRU_PC_SELF_HOSTED_RUNNER:ONLINE'
    exit 0
} catch {
    $payload = [ordered]@{
        state = 'RUNNER_BOOTSTRAP_FAILED'
        repository = $Repository
        runner_name = $RunnerName
        runner_root = $RunnerRoot
        error = $_.Exception.Message
        started_at = $startedAt
        finished_at = (Get-Date).ToString('o')
    }
    try { Write-JsonAtomic -Path $statusPath -Value $payload } catch {}
    try { Post-BootstrapStatus -Payload $payload } catch {}
    Write-Error $_.Exception.Message
    exit 1
}
