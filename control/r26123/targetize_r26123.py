#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, shutil, zipfile
from pathlib import Path

OUTER='MIRU_PC_R2.6.12.3_ACK_SPLIT_TARGETED_SIKCHUNG_WINDOWS_VERIFIED'
PATCH='MIRU_PC_STABILITY_PATCH_R2.6.12.3'
TARGET=r'C:\Users\sikchung\Downloads\MIRU_PC_COMPLETE\MIRU_PC_COMPLETE_V0970_CLEAN1'

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--candidate',type=Path,required=True); ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args(); work=a.out/'targetize-work'
    if work.exists(): shutil.rmtree(work)
    work.mkdir(parents=True); src=work/'src'
    with zipfile.ZipFile(a.candidate) as z: z.extractall(src)
    roots=[p for p in src.rglob(PATCH) if p.is_dir()]
    if len(roots)!=1: raise SystemExit(f'expected one patch root, got {len(roots)}')
    old_patch=roots[0]; outer=work/OUTER; patch=outer/PATCH
    shutil.copytree(old_patch,patch)
    cmd=(
        '@echo off\r\nsetlocal\r\n'
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0payload\\scripts\\Install-MiruPcStabilityPatch.ps1" '
        f'-PatchRoot "%~dp0" -TargetRoot "{TARGET}" %*\r\n'
        'set "RC=%ERRORLEVEL%"\r\necho.\r\n'
        'if not "%RC%"=="0" echo Installation failed with exit code %RC%.\r\n'
        'pause\r\nexit /b %RC%\r\n'
    )
    (patch/'01_INSTALL_MIRU_PC_STABILITY_PATCH.cmd').write_bytes(cmd.encode('ascii'))
    manifest=patch/'MANIFEST.sha256'; lines=[]
    for p in sorted((x for x in patch.rglob('*') if x.is_file() and x != manifest),key=lambda x:x.relative_to(patch).as_posix().lower()):
        lines.append(f'{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.relative_to(patch).as_posix()}')
    manifest.write_bytes(('\r\n'.join(lines)+'\r\n').encode('ascii'))
    a.out.mkdir(parents=True,exist_ok=True)
    out=a.out/(OUTER+'.zip')
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in sorted((x for x in outer.rglob('*') if x.is_file()),key=lambda x:x.relative_to(outer.parent).as_posix().lower()):
            info=zipfile.ZipInfo(p.relative_to(outer.parent).as_posix(),date_time=(2026,7,30,0,0,0)); info.compress_type=zipfile.ZIP_DEFLATED; info.external_attr=(0o644&0xffff)<<16
            z.writestr(info,p.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    sha=hashlib.sha256(out.read_bytes()).hexdigest()
    (a.out/(out.name+'.sha256')).write_text(f'{sha} *{out.name}\n',encoding='ascii')
    print(f'FINAL_ZIP={out}')
    print(f'FINAL_SHA256={sha}')
if __name__=='__main__': main()
