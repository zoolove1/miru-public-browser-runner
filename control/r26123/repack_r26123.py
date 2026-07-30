#!/usr/bin/env python3
from __future__ import annotations
import argparse, base64, gzip, hashlib, json, re, shutil, zipfile
from pathlib import Path

VERSION_OLD='2.6.12.2'
VERSION_NEW='2.6.12.3'
OUTER_NEW='MIRU_PC_R2.6.12.3_ACK_SPLIT_WINDOWS_VERIFIED_CANDIDATE'
PATCH_NEW='MIRU_PC_STABILITY_PATCH_R2.6.12.3'

def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f'{label}: expected one match, found {text.count(old)}')
    return text.replace(old,new,1)

def read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8-sig')

def write_ps(path: Path, text: str) -> None:
    path.write_text(text,encoding='utf-8-sig',newline='\r\n')

def write_cmd(path: Path, text: str) -> None:
    path.write_text(text,encoding='ascii',newline='\r\n')

def decode_gzip_b64(path: Path) -> bytes:
    return gzip.decompress(base64.b64decode(path.read_text(encoding='ascii')))

def build(base_zip: Path, assets: Path, out_dir: Path) -> Path:
    work=out_dir/'work'
    if work.exists(): shutil.rmtree(work)
    work.mkdir(parents=True)
    with zipfile.ZipFile(base_zip) as z: z.extractall(work/'base')
    patch_candidates=[p for p in (work/'base').rglob('MIRU_PC_STABILITY_PATCH_R2.6.12.2') if p.is_dir()]
    if len(patch_candidates)!=1: raise RuntimeError(f'expected one base patch root, got {len(patch_candidates)}')
    base_patch=patch_candidates[0]
    outer=work/OUTER_NEW
    patch=outer/PATCH_NEW
    shutil.copytree(base_patch,patch)
    for p in patch.rglob('*'):
        if p.is_file() and p.suffix.lower() in {'.ps1','.cmd','.txt','.json'} and p.name!='MANIFEST.sha256':
            enc='ascii' if p.suffix.lower()=='.cmd' else 'utf-8-sig'
            text=p.read_text(encoding=enc).replace(VERSION_OLD,VERSION_NEW)
            (write_cmd if p.suffix.lower()=='.cmd' else write_ps)(p,text)
    (patch/'payload/root/Start-MiruSlidesControlAgent.ps1').write_bytes(decode_gzip_b64(assets/'agent.ps1.gz.b64'))
    (patch/'payload/scripts/Test-MiruAckSplit.ps1').write_bytes(decode_gzip_b64(assets/'test.ps1.gz.b64'))

    installer=patch/'payload/scripts/Install-MiruPcStabilityPatch.ps1'
    t=read_text(installer)
    t=replace_once(t,"""$relaySource = Join-Path $source 'root\\Start-MiruContinuousSlidesRelay.ps1'\n$relayTarget = Join-Path $target 'Start-MiruContinuousSlidesRelay.ps1'\n$stateDir = Get-MiruStateDir $target\n""","""$relaySource = Join-Path $source 'root\\Start-MiruContinuousSlidesRelay.ps1'\n$relayTarget = Join-Path $target 'Start-MiruContinuousSlidesRelay.ps1'\n$agentSource = Join-Path $source 'root\\Start-MiruSlidesControlAgent.ps1'\n$agentTarget = Join-Path $target 'Start-MiruSlidesControlAgent.ps1'\n$stateDir = Get-MiruStateDir $target\n""",'installer source/target')
    t=replace_once(t,"""$relayTemp = Join-Path $target ('.Start-MiruContinuousSlidesRelay.' + $transactionId + '.ps1')\n$launcherStageDir""","""$relayTemp = Join-Path $target ('.Start-MiruContinuousSlidesRelay.' + $transactionId + '.ps1')\n$agentTemp = Join-Path $target ('.Start-MiruSlidesControlAgent.' + $transactionId + '.ps1')\n$launcherStageDir""",'installer temp')
    t=replace_once(t,"""$relayExisted = Test-Path -LiteralPath $relayTarget\n$recordExisted""","""$relayExisted = Test-Path -LiteralPath $relayTarget\n$agentExisted = Test-Path -LiteralPath $agentTarget\n$recordExisted""",'installer existed')
    t=replace_once(t,"""    try { Remove-Item -LiteralPath $relayTemp -Force -ErrorAction SilentlyContinue } catch { $rollbackErrors.Add($_.Exception.Message) }\n    try {\n""","""    try { Remove-Item -LiteralPath $relayTemp -Force -ErrorAction SilentlyContinue } catch { $rollbackErrors.Add($_.Exception.Message) }\n    try { Remove-Item -LiteralPath $agentTemp -Force -ErrorAction SilentlyContinue } catch { $rollbackErrors.Add($_.Exception.Message) }\n    try {\n""",'rollback temp')
    t=replace_once(t,"""    } catch { $rollbackErrors.Add('relay: ' + $_.Exception.Message) }\n    try { Restore-LauncherState } catch { $rollbackErrors.Add('launchers: ' + $_.Exception.Message) }\n""","""    } catch { $rollbackErrors.Add('relay: ' + $_.Exception.Message) }\n    try {\n        $agentBackup = Join-Path $backupDir 'Start-MiruSlidesControlAgent.ps1'\n        if ($agentExisted) {\n            if (-not (Test-Path -LiteralPath $agentBackup)) { throw 'control agent backup missing' }\n            Copy-Item -LiteralPath $agentBackup -Destination $agentTarget -Force\n        } else { Remove-Item -LiteralPath $agentTarget -Force -ErrorAction SilentlyContinue }\n    } catch { $rollbackErrors.Add('control-agent: ' + $_.Exception.Message) }\n    try { Restore-LauncherState } catch { $rollbackErrors.Add('launchers: ' + $_.Exception.Message) }\n""",'rollback agent')
    t=replace_once(t,"""if ($relayExisted) { Copy-Item -LiteralPath $relayTarget -Destination (Join-Path $backupDir 'Start-MiruContinuousSlidesRelay.ps1') -Force }\nif (Test-Path -LiteralPath $installDir)""","""if ($relayExisted) { Copy-Item -LiteralPath $relayTarget -Destination (Join-Path $backupDir 'Start-MiruContinuousSlidesRelay.ps1') -Force }\nif ($agentExisted) { Copy-Item -LiteralPath $agentTarget -Destination (Join-Path $backupDir 'Start-MiruSlidesControlAgent.ps1') -Force }\nif (Test-Path -LiteralPath $installDir)""",'backup agent')
    t=replace_once(t,"""Copy-Item -LiteralPath $relaySource -Destination $relayTemp -Force\n\n    $commitStarted""","""Copy-Item -LiteralPath $relaySource -Destination $relayTemp -Force\nCopy-Item -LiteralPath $agentSource -Destination $agentTemp -Force\n\n    $commitStarted""",'stage agent')
    t=replace_once(t,"""    Move-Item -LiteralPath $relayTemp -Destination $relayTarget -Force\n    if ($FaultInjectionStep -eq 'AfterCoreReplace')""","""    Move-Item -LiteralPath $relayTemp -Destination $relayTarget -Force\n    Move-Item -LiteralPath $agentTemp -Destination $agentTarget -Force\n    if ($FaultInjectionStep -eq 'AfterCoreReplace')""",'activate agent')
    t=replace_once(t,"""      continuous_slides_relay_backup = if ($relayExisted) { (Join-Path $backupDir 'Start-MiruContinuousSlidesRelay.ps1') } else { '' }\n      continuous_slides_relay_installed = $true\n""","""      continuous_slides_relay_backup = if ($relayExisted) { (Join-Path $backupDir 'Start-MiruContinuousSlidesRelay.ps1') } else { '' }\n      control_agent_backup = if ($agentExisted) { (Join-Path $backupDir 'Start-MiruSlidesControlAgent.ps1') } else { '' }\n      control_agent_installed = $true\n      control_agent_version = '0.9.9.2'\n      ack_operation_frame_split = $true\n      ack_soft_frame_error_state = 'SUCCEEDED_WITH_FRAME_ERROR'\n      ack_frame_only_failure_state = 'FAILED'\n      continuous_slides_relay_installed = $true\n""",'record agent')
    t=replace_once(t,"""    Remove-Item -LiteralPath $relayTemp -Force -ErrorAction SilentlyContinue\n}\n\nWrite-Host""","""    Remove-Item -LiteralPath $relayTemp -Force -ErrorAction SilentlyContinue\n    Remove-Item -LiteralPath $agentTemp -Force -ErrorAction SilentlyContinue\n}\n\nWrite-Host""",'cleanup agent')
    t=replace_once(t,"""Write-Host 'Core input engine remains v0.9.9.1; the stack supervisor and continuous Slides relay were transactionally replaced.' -ForegroundColor Green\nWrite-Host 'Next: run 44_START_MIRU_PC_STABLE_MODE.cmd. The control plane remains available if Slides is degraded; verify all three channels after startup.'\n""","""Write-Host 'Control agent v0.9.9.2, stack supervisor, and continuous Slides relay were transactionally replaced.' -ForegroundColor Green\nWrite-Host 'ACK now separates command receipt, operation execution, and result-frame status. Input success is preserved when only the result frame fails.' -ForegroundColor Green\nWrite-Host 'Next: run 44_START_MIRU_PC_STABLE_MODE.cmd and verify one input command plus one FRQ command.'\n""",'installer output')
    write_ps(installer,t)

    rb=patch/'payload/scripts/Test-MiruInstallerRollback.ps1';t=read_text(rb)
    t=replace_once(t,"""    [IO.File]::WriteAllText((Join-Path $target 'Start-MiruContinuousSlidesRelay.ps1'),'ORIGINAL_RELAY',[Text.UTF8Encoding]::new($false))\n    [IO.File]::WriteAllText((Join-Path $target 'MIRU_PC_STABILITY_PATCH_R2\\original.txt')""","""    [IO.File]::WriteAllText((Join-Path $target 'Start-MiruContinuousSlidesRelay.ps1'),'ORIGINAL_RELAY',[Text.UTF8Encoding]::new($false))\n    [IO.File]::WriteAllText((Join-Path $target 'Start-MiruSlidesControlAgent.ps1'),'ORIGINAL_AGENT',[Text.UTF8Encoding]::new($false))\n    [IO.File]::WriteAllText((Join-Path $target 'MIRU_PC_STABILITY_PATCH_R2\\original.txt')""",'rollback fixture')
    t=replace_once(t,"""    if ((Get-Content -LiteralPath (Join-Path $target 'Start-MiruContinuousSlidesRelay.ps1') -Raw) -ne 'ORIGINAL_RELAY') { throw ('relay rollback failed: '+$FaultStep) }\n    if (-not (Test-Path""","""    if ((Get-Content -LiteralPath (Join-Path $target 'Start-MiruContinuousSlidesRelay.ps1') -Raw) -ne 'ORIGINAL_RELAY') { throw ('relay rollback failed: '+$FaultStep) }\n    if ((Get-Content -LiteralPath (Join-Path $target 'Start-MiruSlidesControlAgent.ps1') -Raw) -ne 'ORIGINAL_AGENT') { throw ('control-agent rollback failed: '+$FaultStep) }\n    if (-not (Test-Path""",'rollback assert')
    write_ps(rb,t)

    st=patch/'payload/scripts/Test-MiruPatchStatic.ps1';t=read_text(st)
    t=replace_once(t,"'payload\\core\\Start-MiruSingleWindowStack.ps1','payload\\root\\Start-MiruContinuousSlidesRelay.ps1','payload\\scripts\\Common.ps1'","'payload\\core\\Start-MiruSingleWindowStack.ps1','payload\\root\\Start-MiruContinuousSlidesRelay.ps1','payload\\root\\Start-MiruSlidesControlAgent.ps1','payload\\scripts\\Common.ps1'",'static required agent')
    t=replace_once(t,"'payload\\scripts\\Test-MiruTerminationPropagation.ps1','payload\\scripts\\Uninstall-MiruPcStabilityPatch.ps1')","'payload\\scripts\\Test-MiruTerminationPropagation.ps1','payload\\scripts\\Test-MiruAckSplit.ps1','payload\\scripts\\Uninstall-MiruPcStabilityPatch.ps1')",'static required test')
    anchor="$overlay=Has 'payload\\scripts\\Miru-OverlaySupervisor.ps1'"
    checks="""$agent=Has 'payload\\root\\Start-MiruSlidesControlAgent.ps1' @("$AgentVersion = '0.9.9.2'",'Get-MiruAckOutcome','Test-FrameDeliveryIsPrimaryOperation','SUCCEEDED_WITH_FRAME_ERROR','overall_status = $State','command_received = $true','operation_status = $OperationStatus','frame_status = $FrameStatus','COMMAND_SUCCEEDED_WITH_FRAME_ERROR')\n$ackRegression=Join-Path $PatchRoot 'payload\\scripts\\Test-MiruAckSplit.ps1'\ntry{& (Get-Process -Id $PID).Path -NoProfile -ExecutionPolicy Bypass -File $ackRegression -PatchRoot $PatchRoot;if($LASTEXITCODE-ne0){Fail 'ACK split regression failed'}}catch{Fail ('ACK split regression exception: '+$_.Exception.Message)}\n"""
    if t.count(anchor)!=1: raise RuntimeError('static insertion anchor')
    t=t.replace(anchor,checks+anchor,1)
    t=replace_once(t,"'continuous_slides_target_interval_ms = 200')","'continuous_slides_target_interval_ms = 200','agentSource','agentTarget','agentTemp','control_agent_version = ''0.9.9.2''','ack_operation_frame_split = $true','SUCCEEDED_WITH_FRAME_ERROR')",'installer static tokens')
    t=replace_once(t,"Write-Host 'Manifest, AST, independent control plane, latest-only Slides thumbnail, Drive original frame, 200 ms pacing, ownership, rollback, and packaging contracts: PASS'","Write-Host 'Manifest, AST, ACK operation/frame split, independent control plane, latest-only Slides thumbnail, Drive original frame, 200 ms pacing, ownership, rollback, and packaging contracts: PASS'",'static success')
    write_ps(st,t)

    readme=patch/'00_README_FIRST.txt'; write_ps(readme,read_text(readme)+"\nR2.6.12.3 ACK SPLIT\n- control agent v0.9.9.2 is installed transactionally\n- input success plus result-frame failure => SUCCEEDED_WITH_FRAME_ERROR\n- FRQ/FRQF frame failure => FAILED\n- ACK fields: command_received, operation_status, frame_status, frame_error, overall_status\n")
    write_ps(patch/'STATIC_VALIDATION.txt','STATIC VALIDATION MUST PASS BEFORE INSTALLATION.\nR2.6.12.3 adds transactional control-agent replacement and ACK operation/frame split regression.\n')
    write_ps(patch/'VERSION.txt',VERSION_NEW+'\n')

    manifest=patch/'MANIFEST.sha256';lines=[]
    for p in sorted([x for x in patch.rglob('*') if x.is_file() and x!=manifest],key=lambda x:x.relative_to(patch).as_posix().lower()):
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.relative_to(patch).as_posix()}")
    manifest.write_text('\r\n'.join(lines)+'\r\n',encoding='ascii',newline='')

    out_dir.mkdir(parents=True,exist_ok=True)
    out_zip=out_dir/'MIRU_PC_R2.6.12.3_ACK_SPLIT_WINDOWS_VERIFIED_CANDIDATE.zip'
    with zipfile.ZipFile(out_zip,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in sorted(outer.rglob('*'),key=lambda x:x.relative_to(outer.parent).as_posix().lower()):
            if p.is_file():
                info=zipfile.ZipInfo(p.relative_to(outer.parent).as_posix(),date_time=(2026,7,30,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=(0o644&0xffff)<<16
                z.writestr(info,p.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    sha=hashlib.sha256(out_zip.read_bytes()).hexdigest()
    (out_dir/(out_zip.name+'.sha256')).write_text(f'{sha} *{out_zip.name}\n',encoding='ascii')
    print(json.dumps({'zip':str(out_zip),'sha256':sha,'files':len(lines)+1}))
    return out_zip

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base-zip',type=Path,required=True);ap.add_argument('--assets',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args();build(a.base_zip,a.assets,a.out)
if __name__=='__main__':main()
