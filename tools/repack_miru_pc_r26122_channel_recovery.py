from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

VERSION = '2.6.12.2'
CORE_VERSION = '0.9.9.3'
RELAY_MODE = 'SLIDES_THUMBNAIL_LATEST_ONLY'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8-sig')


def write_text(path: Path, text: str) -> None:
    path.write_text(text.replace('\r\n', '\n').replace('\r', '\n'), encoding='utf-8', newline='\n')


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one occurrence, found {count}')
    return text.replace(old, new, 1)


def replace_required(text: str, old: str, new: str, label: str, minimum: int = 1) -> str:
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f'{label}: expected at least {minimum}, found {count}')
    return text.replace(old, new)


def patch_core(path: Path) -> None:
    text = read_text(path)
    text = replace_required(text, "0.9.9.2", CORE_VERSION, 'core version')
    old_predicate = """function Test-RelayReady {
    if (-not (Test-Path $relayStatus)) { return $false }
    try {
        $s=Get-Content -Raw $relayStatus|ConvertFrom-Json
        # RELAY_PRODUCER_CONSUMER_MODE_MATCH
        return (
            [string]$s.state -eq 'READY' -and
            [string]$s.mode -eq 'ORIGINAL_RESOLUTION_PACED_SINGLE_FLIGHT' -and
            [bool]$s.original_resolution -eq $true -and
            [bool]$s.resampling -eq $false -and
            [string]$s.image_format -eq 'PNG' -and
            [int]$s.target_interval_ms -eq 200 -and
            [string]$s.backpressure_mode -eq 'SINGLE_IN_FLIGHT_NO_QUEUE'
        )
    } catch { return $false }
}
"""
    new_predicate = """function Test-RelayReady {
    if (-not (Test-Path $relayStatus)) { return $false }
    try {
        $s=Get-Content -Raw $relayStatus|ConvertFrom-Json
        # RELAY_PRODUCER_CONSUMER_MODE_MATCH_R26122
        return (
            [string]$s.state -eq 'READY' -and
            [string]$s.mode -eq 'SLIDES_THUMBNAIL_LATEST_ONLY' -and
            [bool]$s.channel_independent -eq $true -and
            [bool]$s.latest_frame_only -eq $true -and
            [string]$s.slide_payload -eq 'DOWNSCALED_JPEG_THUMBNAIL' -and
            [string]$s.original_frame_route -eq 'DRIVE_RAW_FRAME' -and
            [string]$s.image_format -eq 'JPEG' -and
            [int]$s.target_interval_ms -eq 200 -and
            [string]$s.backpressure_mode -eq 'SINGLE_IN_FLIGHT_NO_QUEUE'
        )
    } catch { return $false }
}

function Wait-RelayReadyNonBlocking([Diagnostics.Process]$Process,[int]$Seconds) {
    $deadline=(Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            Write-StackLog ('SLIDES_RELAY_CHANNEL_DEGRADED exited_during_startup code='+$Process.ExitCode)
            return $false
        }
        if (Test-RelayReady) {
            Write-StackLog 'SLIDES_RELAY_READY'
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    $message='readiness timeout'
    try {
        if(Test-Path -LiteralPath $relayStatus){
            $relayState=Get-Content -Raw -LiteralPath $relayStatus|ConvertFrom-Json
            if($relayState.PSObject.Properties['message']){$message=[string]$relayState.message}
        }
    } catch {}
    Write-StackLog ('SLIDES_RELAY_CHANNEL_DEGRADED message='+$message)
    return $false
}
"""
    text = replace_once(text, old_predicate, new_predicate, 'relay predicate')

    old_start = """    $children.relay=Start-HiddenPowerShell -Name 'relay' -Script $relayScript -ExtraArgs @(
        '-TargetIntervalMilliseconds','200',
        '-ImageFormat','PNG'
    )
    Wait-Ready -Name 'SLIDES_RELAY' -Process $children.relay -Predicate ${function:Test-RelayReady} -Seconds 30
    $script:relayReady=$true
"""
    new_start = """    # SLIDES_RELAY_CHANNEL_INDEPENDENT_R26122
    $children.relay=Start-HiddenPowerShell -Name 'relay' -Script $relayScript -ExtraArgs @(
        '-TargetIntervalMilliseconds','200',
        '-ThumbnailWidth','640',
        '-JpegQuality','30'
    )
    $script:relayReady=Wait-RelayReadyNonBlocking -Process $children.relay -Seconds 25
"""
    text = replace_once(text, old_start, new_start, 'relay startup block')

    old_status = """    Write-JsonAtomic -Path $supervisorStatus -Object ([ordered]@{
        version='0.9.9.3'
        state='READY'
        mode='SINGLE_VISIBLE_SUPERVISOR'
        listener_port=$port
        bridge_pid=$children.bridge.Id
        poller_pid=$children.poller.Id
        agent_pid=$children.agent.Id
        focus_pid=$children.focus.Id
        relay_pid=$children.relay.Id
        relay_state='READY'
        relay_required=$true
        relay_mode='ORIGINAL_RESOLUTION_PACED_SINGLE_FLIGHT'
        relay_original_resolution=$true
        relay_resampling=$false
        relay_image_format='PNG'
        relay_target_interval_ms=200
        relay_backpressure_mode='SINGLE_IN_FLIGHT_NO_QUEUE'
        started_at=(Get-Date).ToString('o')
    })
"""
    new_status = """    Write-JsonAtomic -Path $supervisorStatus -Object ([ordered]@{
        version='0.9.9.3'
        state=if($script:relayReady){'READY'}else{'READY_WITH_SLIDES_DEGRADED'}
        mode='SINGLE_VISIBLE_SUPERVISOR'
        listener_port=$port
        bridge_pid=$children.bridge.Id
        poller_pid=$children.poller.Id
        agent_pid=$children.agent.Id
        focus_pid=$children.focus.Id
        relay_pid=$children.relay.Id
        relay_state=if($script:relayReady){'READY'}else{'DEGRADED'}
        relay_channel_independent=$true
        relay_thumbnail_mode=$true
        relay_mode='SLIDES_THUMBNAIL_LATEST_ONLY'
        relay_slide_payload='DOWNSCALED_JPEG_THUMBNAIL'
        relay_original_frame_route='DRIVE_RAW_FRAME'
        relay_image_format='JPEG'
        relay_thumbnail_width=640
        relay_jpeg_quality=30
        relay_target_interval_ms=200
        relay_backpressure_mode='SINGLE_IN_FLIGHT_NO_QUEUE'
        started_at=(Get-Date).ToString('o')
        updated_at=(Get-Date).ToString('o')
    })
"""
    text = replace_once(text, old_status, new_status, 'supervisor status block')

    old_print = """    Write-Host (
        'Slides relay PID: '+$children.relay.Id+
        ' (original resolution, lossless PNG, 200 ms target, single-flight/no queue)'
    ) -ForegroundColor Cyan
"""
    new_print = """    if($script:relayReady){
        Write-Host ('Slides relay PID: '+$children.relay.Id+' (latest-only JPEG thumbnail READY; original PNG via Drive raw-frame)') -ForegroundColor Cyan
    } else {
        Write-Host ('Slides relay PID: '+$children.relay.Id+' (DEGRADED; wormhole and Actions remain available while relay retries)') -ForegroundColor Yellow
    }
"""
    text = replace_once(text, old_print, new_print, 'relay print block')

    old_loop = """    while (-not (Test-Path $supervisorStop)) {
        foreach ($key in @('bridge','poller','agent','focus','relay')) {
            $p=$children[$key]
            $p.Refresh()
            if ($p.HasExited) {
                throw ('Monitored essential child exited: '+$key+' exit_code='+$p.ExitCode)
            }
        }
        Start-Sleep -Seconds 2
    }
"""
    new_loop = """    while (-not (Test-Path $supervisorStop)) {
        foreach ($key in @('bridge','poller','agent','focus')) {
            $p=$children[$key]
            $p.Refresh()
            if ($p.HasExited) {
                throw ('Monitored essential child exited: '+$key+' exit_code='+$p.ExitCode)
            }
        }

        $relayProcess=$children.relay
        $relayProcess.Refresh()
        if($relayProcess.HasExited){
            Write-StackLog ('SLIDES_RELAY_RESTART exit_code='+$relayProcess.ExitCode)
            $children.relay=Start-HiddenPowerShell -Name 'relay' -Script $relayScript -ExtraArgs @('-TargetIntervalMilliseconds','200','-ThumbnailWidth','640','-JpegQuality','30')
            $script:relayReady=$false
        } elseif(Test-RelayReady) {
            if(-not $script:relayReady){Write-StackLog 'SLIDES_RELAY_RECOVERED'}
            $script:relayReady=$true
        } else {
            $script:relayReady=$false
        }

        Write-JsonAtomic -Path $supervisorStatus -Object ([ordered]@{
            version='0.9.9.3'
            state=if($script:relayReady){'READY'}else{'READY_WITH_SLIDES_DEGRADED'}
            mode='SINGLE_VISIBLE_SUPERVISOR'
            listener_port=$port
            bridge_pid=$children.bridge.Id
            poller_pid=$children.poller.Id
            agent_pid=$children.agent.Id
            focus_pid=$children.focus.Id
            relay_pid=$children.relay.Id
            relay_state=if($script:relayReady){'READY'}else{'DEGRADED'}
            relay_channel_independent=$true
            relay_thumbnail_mode=$true
            relay_mode='SLIDES_THUMBNAIL_LATEST_ONLY'
            relay_slide_payload='DOWNSCALED_JPEG_THUMBNAIL'
            relay_original_frame_route='DRIVE_RAW_FRAME'
            relay_image_format='JPEG'
            relay_thumbnail_width=640
            relay_jpeg_quality=30
            relay_target_interval_ms=200
            relay_backpressure_mode='SINGLE_IN_FLIGHT_NO_QUEUE'
            started_at=$startedAt
            updated_at=(Get-Date).ToString('o')
        })
        Start-Sleep -Seconds 2
    }
"""
    # Need a stable startedAt value in main scope.
    text = replace_once(text, "    Start-AllChildren\n\n", "    Start-AllChildren\n    $startedAt=(Get-Date).ToString('o')\n\n", 'main startedAt')
    text = replace_once(text, old_loop, new_loop, 'supervisor monitor loop')
    write_text(path, text)


def patch_stable_start(path: Path) -> None:
    text = read_text(path)
    text = replace_required(text, "0.9.9.2", CORE_VERSION, 'stable core version')
    text = replace_required(text, "ORIGINAL_RESOLUTION_PACED_SINGLE_FLIGHT", RELAY_MODE, 'stable relay mode')
    old_contract = """    return (
      [string](Get-Value $status 'state') -eq 'READY' -and
      [string](Get-Value $status 'version') -eq $requiredCoreVersion -and
      [string](Get-Value $status 'relay_state') -eq 'READY' -and
      [string](Get-Value $status 'relay_mode') -eq $requiredRelayMode -and
      [bool](Get-Value $status 'relay_original_resolution') -eq $true -and
      [bool](Get-Value $status 'relay_resampling') -eq $false -and
      [string](Get-Value $status 'relay_image_format') -eq 'PNG' -and
      [int](Get-Value $status 'relay_target_interval_ms') -eq 200 -and
      [string](Get-Value $status 'relay_backpressure_mode') -eq 'SINGLE_IN_FLIGHT_NO_QUEUE'
    )
"""
    new_contract = """    $state=[string](Get-Value $status 'state')
    return (
      $state -in @('READY','READY_WITH_SLIDES_DEGRADED') -and
      [string](Get-Value $status 'version') -eq $requiredCoreVersion -and
      [bool](Get-Value $status 'relay_channel_independent') -eq $true -and
      [string](Get-Value $status 'relay_mode') -eq $requiredRelayMode -and
      [string](Get-Value $status 'relay_original_frame_route') -eq 'DRIVE_RAW_FRAME' -and
      [int](Get-Value $status 'relay_target_interval_ms') -eq 200 -and
      [string](Get-Value $status 'relay_backpressure_mode') -eq 'SINGLE_IN_FLIGHT_NO_QUEUE'
    )
"""
    text = replace_once(text, old_contract, new_contract, 'stable current contract')
    text = replace_required(text, 'R2.6.12.1', 'R'+VERSION, 'stable version text')
    text = replace_required(text, 'CORE_STACK_R2612', 'CORE_STACK_R26122', 'stable marker')
    write_text(path, text)


def patch_static(path: Path) -> None:
    text = read_text(path)
    text = replace_required(text, "2.6.12.1", VERSION, 'static version')
    old_core = "$core=Has 'payload\\core\\Start-MiruSingleWindowStack.ps1' @('CommandLineToArgvW','Get-StackCommandLineOwnershipState','Test-StackCommandLineOwned','Join-Path $expectedRoot $knownName','.Equals($expectedPath','OLD_STACK_INSPECTION_INDETERMINATE','OLD_STACK_COMMAND_LINE_INDETERMINATE','Stop-StackOwnedProcess','OpenProcess','GetProcessTimes','TerminateProcess','WaitForSingleObject','WAIT_OBJECT_0','$waitResult -ne [MiruStackProcessNative]::WAIT_OBJECT_0','[void]$p.Handle','$p.Kill()','SUPERVISOR_DUPLICATE_EXIT_NO_CLEANUP','SLIDES_RELAY_REQUIRED_FILE_MISSING','relay_required=$true','relay_original_resolution=$true','relay_target_interval_ms=200','SINGLE_IN_FLIGHT_NO_QUEUE')"
    new_core = "$core=Has 'payload\\core\\Start-MiruSingleWindowStack.ps1' @('CommandLineToArgvW','Get-StackCommandLineOwnershipState','Test-StackCommandLineOwned','Join-Path $expectedRoot $knownName','.Equals($expectedPath','OLD_STACK_INSPECTION_INDETERMINATE','OLD_STACK_COMMAND_LINE_INDETERMINATE','Stop-StackOwnedProcess','OpenProcess','GetProcessTimes','TerminateProcess','WaitForSingleObject','WAIT_OBJECT_0','$waitResult -ne [MiruStackProcessNative]::WAIT_OBJECT_0','[void]$p.Handle','$p.Kill()','SUPERVISOR_DUPLICATE_EXIT_NO_CLEANUP','SLIDES_RELAY_REQUIRED_FILE_MISSING','SLIDES_RELAY_CHANNEL_INDEPENDENT_R26122','relay_channel_independent=$true','relay_thumbnail_mode=$true','relay_original_frame_route=''DRIVE_RAW_FRAME'','relay_target_interval_ms=200','SINGLE_IN_FLIGHT_NO_QUEUE')"
    text = replace_once(text, old_core, new_core, 'static core tokens')
    start = text.index("$relay=Has 'payload\\