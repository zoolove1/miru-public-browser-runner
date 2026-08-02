[CmdletBinding()]
param(
    [string]$EventPath = $env:GITHUB_EVENT_PATH
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$fixedRepository = 'zoolove1/useful-orbit-automation'
$fixedIssue = 290
$fixedSender = 'zoolove1'
$protocol = 'MIRU-RUNNER/1'
$stateDir = Join-Path $env:LOCALAPPDATA 'MIRU\runner-control'
$statePath = Join-Path $stateDir 'dispatcher-state.json'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

function Write-JsonAtomic {
    param([Parameter(Mandatory)][string]$Path,[Parameter(Mandatory)]$Value)
    $tmp = $Path + '.tmp.' + [guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText($tmp,($Value | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Read-State {
    if (-not (Test-Path -LiteralPath $statePath)) {
        return [pscustomobject][ordered]@{ processed = @(); last_command_id = $null; updated_at = $null }
    }
    try { return (Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json) }
    catch { return [pscustomobject][ordered]@{ processed = @(); last_command_id = $null; updated_at = $null } }
}

function Get-CommandFromBody {
    param([Parameter(Mandatory)][string]$Body)
    $lines = $Body -split "`r?`n"
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq 'MIRU_RUNNER_SLOT:') {
            if (($i + 1) -ge $lines.Count) { throw 'MIRU_RUNNER_SLOT JSON line is missing.' }
            return ($lines[$i + 1].Trim() | ConvertFrom-Json)
        }
    }
    throw 'MIRU_RUNNER_SLOT marker was not found.'
}

function Post-Ack {
    param([Parameter(Mandatory)]$Payload)
    $temp = Join-Path $stateDir ('runner-ack-' + [guid]::NewGuid().ToString('N') + '.txt')
    try {
        $body = "MIRU_RUNNER_ACK:`n" + ($Payload | ConvertTo-Json -Depth 20 -Compress)
        [IO.File]::WriteAllText($temp,$body,[Text.UTF8Encoding]::new($false))
        & gh issue comment $fixedIssue --repo $fixedRepository --body-file $temp | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to post MIRU_RUNNER_ACK.' }
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

function Get-ToolVersion {
    param([Parameter(Mandatory)][string]$FilePath,[string[]]$Arguments = @('--version'))
    try {
        $text = & $FilePath @Arguments 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($text | Select-Object -First 1) | Out-String).Trim()
    } catch { return $null }
}

function Invoke-Preflight {
    $probeDir = Join-Path $stateDir ('preflight-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
    try {
        $probeFile = Join-Path $probeDir 'probe.txt'
        $probeText = 'MIRU-RUNNER-PREFLIGHT-' + [guid]::NewGuid().ToString('N')
        [IO.File]::WriteAllText($probeFile,$probeText,[Text.UTF8Encoding]::new($false))
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $probeFile).Hash.ToLowerInvariant()
        $readback = [IO.File]::ReadAllText($probeFile,[Text.Encoding]::UTF8)
        if ($readback -ne $probeText) { throw 'File readback mismatch.' }

        $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d','/c','exit 0') -PassThru -Wait -WindowStyle Hidden
        if ([int]$proc.ExitCode -ne 0) { throw 'Process start/exit test failed.' }

        $sessionId = (Get-Process -Id $PID).SessionId
        $interactiveUser = $null
        try { $interactiveUser = [string](Get-CimInstance Win32_ComputerSystem).UserName } catch {}

        return [ordered]@{
            whoami = (& whoami | Out-String).Trim()
            interactive_user = $interactiveUser
            process_session_id = $sessionId
            current_directory = (Get-Location).Path
            runner_name = [string]$env:RUNNER_NAME
            runner_os = [string]$env:RUNNER_OS
            runner_arch = [string]$env:RUNNER_ARCH
            powershell = $PSVersionTable.PSVersion.ToString()
            git = Get-ToolVersion -FilePath 'git'
            gh = Get-ToolVersion -FilePath 'gh'
            python = Get-ToolVersion -FilePath 'python'
            file_sha256 = $hash
            file_readback = 'PASS'
            process_start_stop = 'PASS'
        }
    } finally {
        Remove-Item -LiteralPath $probeDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-TaskScriptBytes {
    param(
        [Parameter(Mandatory)][string]$Ref,
        [Parameter(Mandatory)][string]$Path
    )

    $encodedRef = [uri]::EscapeDataString($Ref)
    $apiPath = "repos/$fixedRepository/contents/$Path`?ref=$encodedRef"
    $jsonText = & gh api $apiPath 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ('Task script API read failed: ' + (($jsonText | Out-String).Trim()))
    }
    $resource = (($jsonText | Out-String).Trim() | ConvertFrom-Json)
    if ([string]$resource.type -ne 'file' -or [string]$resource.encoding -ne 'base64') {
        throw 'Task script API response was not a base64 file.'
    }
    $base64 = ([string]$resource.content) -replace '\s',''
    if ([string]::IsNullOrWhiteSpace($base64)) { throw 'Task script content was empty.' }
    return [Convert]::FromBase64String($base64)
}

function Invoke-RunnerTask {
    param([Parameter(Mandatory)]$ArgsObject)

    $ref = [string]$ArgsObject.ref
    $path = [string]$ArgsObject.path
    if ($ref -notmatch '^control/miru-pc-runner-task-[A-Za-z0-9._-]+$') {
        throw 'Task ref is outside the allowed control/miru-pc-runner-task-* namespace.'
    }
    if ($path -notmatch '^runner-tasks/[A-Za-z0-9._/-]+\.ps1$' -or $path.Contains('..')) {
        throw 'Task path is outside runner-tasks/*.ps1.'
    }

    $taskBytes = Get-TaskScriptBytes -Ref $ref -Path $path
    $taskDir = Join-Path $stateDir ('task-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $taskDir -Force | Out-Null
    try {
        $taskPath = Join-Path $taskDir 'task.ps1'
        $argsPath = Join-Path $taskDir 'args.json'
        [IO.File]::WriteAllBytes($taskPath,$taskBytes)
        [IO.File]::WriteAllText($argsPath,($ArgsObject | ConvertTo-Json -Depth 20),[Text.UTF8Encoding]::new($false))

        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $taskPath -ArgsJsonPath $argsPath 2>&1
        $exitCode = $LASTEXITCODE
        $lines = @($output | ForEach-Object { [string]$_ })
        $tail = @($lines | Select-Object -Last 80)
        if ($exitCode -ne 0) {
            throw ('Runner task failed with exit code ' + $exitCode + ': ' + (($tail | Out-String).Trim()))
        }

        return [ordered]@{
            ref = $ref
            path = $path
            exit_code = $exitCode
            stdout_tail = $tail
        }
    } finally {
        Remove-Item -LiteralPath $taskDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$startedAt = (Get-Date).ToString('o')
$commandId = $null
try {
    if ([string]::IsNullOrWhiteSpace($EventPath) -or -not (Test-Path -LiteralPath $EventPath)) {
        throw 'GITHUB_EVENT_PATH was not found.'
    }
    $event = Get-Content -Raw -LiteralPath $EventPath | ConvertFrom-Json
    if ([string]$event.repository.full_name -ne $fixedRepository) { throw 'Repository scope mismatch.' }
    if ([int]$event.issue.number -ne $fixedIssue) { throw 'Issue scope mismatch.' }
    if ([string]$event.sender.login -ne $fixedSender) { throw 'Sender scope mismatch.' }

    $command = Get-CommandFromBody -Body ([string]$event.issue.body)
    $commandId = [string]$command.command_id
    if ($commandId -notmatch '^[A-Za-z0-9._:-]{8,120}$') { throw 'Invalid command_id.' }
    if ([string]$command.protocol -ne $protocol) { throw 'Protocol mismatch.' }
    if ($commandId -eq 'EMPTY') { exit 0 }

    $issued = [DateTimeOffset]::Parse([string]$command.issued_at)
    $ageSeconds = ([DateTimeOffset]::Now - $issued).TotalSeconds
    if ($ageSeconds -lt -60 -or $ageSeconds -gt 1800) { throw 'Command is expired or too far in the future.' }

    $state = Read-State
    $processed = @($state.processed | ForEach-Object { [string]$_ })
    if ($processed -contains $commandId) {
        Post-Ack -Payload ([ordered]@{
            command_id = $commandId
            route_used = 'RUNNER'
            status = 'DUPLICATE_SKIPPED'
            exit_code = 0
            started_at = $startedAt
            finished_at = (Get-Date).ToString('o')
        })
        exit 0
    }

    $state.processed = @($processed | Select-Object -Last 199) + @($commandId)
    $state.last_command_id = $commandId
    $state.updated_at = (Get-Date).ToString('o')
    Write-JsonAtomic -Path $statePath -Value $state

    $op = ([string]$command.op).ToUpperInvariant()
    $result = switch ($op) {
        'NOP' { [ordered]@{ action = 'noop' } }
        'PREFLIGHT' { Invoke-Preflight }
        'RUN_TASK' { Invoke-RunnerTask -ArgsObject $command.args }
        default { throw ('Unsupported runner op: ' + $op) }
    }

    $ack = [ordered]@{
        command_id = $commandId
        route_used = 'RUNNER'
        status = 'SUCCEEDED'
        exit_code = 0
        started_at = $startedAt
        finished_at = (Get-Date).ToString('o')
        runner_name = [string]$env:RUNNER_NAME
        result = $result
    }
    Post-Ack -Payload $ack
    Write-Host ('MIRU_RUNNER_ACK:' + ($ack | ConvertTo-Json -Depth 20 -Compress))
    exit 0
} catch {
    $ack = [ordered]@{
        command_id = $commandId
        route_used = 'RUNNER'
        status = 'FAILED'
        exit_code = 1
        started_at = $startedAt
        finished_at = (Get-Date).ToString('o')
        runner_name = [string]$env:RUNNER_NAME
        error = $_.Exception.Message
    }
    try { Post-Ack -Payload $ack } catch {}
    Write-Error $_.Exception.Message
    exit 1
}
