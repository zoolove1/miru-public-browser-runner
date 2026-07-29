from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path

VERSION = '2.6.12.1'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_utf8(path: Path, text: str) -> None:
    path.write_text(text.replace('\r\n', '\n').replace('\r', '\n'), encoding='utf-8', newline='\n')


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one occurrence, found {count}')
    return text.replace(old, new, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('input_zip')
    parser.add_argument('output_zip')
    args = parser.parse_args()
    source = Path(args.input_zip).resolve()
    output = Path(args.output_zip).resolve()

    with tempfile.TemporaryDirectory(prefix='miru-r26121-recovery-') as temporary:
        temporary_root = Path(temporary)
        extracted = temporary_root / 'extracted'
        final_root = temporary_root / 'MIRU_PC_R2.6.12.1_RECOVERY_WINDOWS_VERIFIED'
        with zipfile.ZipFile(source) as archive:
            archive.extractall(extracted)

        candidates = [
            path.parent
            for path in extracted.rglob('VERSION.txt')
            if (path.parent / 'payload/scripts/Test-MiruPatchStatic.ps1').exists()
        ]
        if len(candidates) != 1:
            raise RuntimeError(f'patch root count={len(candidates)}')

        patch_root = final_root / 'MIRU_PC_STABILITY_PATCH_R2.6.12.1'
        shutil.copytree(candidates[0], patch_root)

        write_utf8(patch_root / 'VERSION.txt', VERSION + '\n')

        static = patch_root / 'payload/scripts/Test-MiruPatchStatic.ps1'
        static_text = static.read_text(encoding='utf-8-sig')
        static_text = replace_once(
            static_text,
            ".Trim()-ne '2.6.12'",
            ".Trim()-ne '2.6.12.1'",
            'static version contract',
        )
        write_utf8(static, static_text)

        readme = patch_root / '00_README_FIRST.txt'
        readme_text = readme.read_text(encoding='utf-8-sig')
        readme_text = readme_text.replace(
            'MIRU PC STABILITY PATCH R2.6.12\n',
            'MIRU PC STABILITY PATCH R2.6.12.1\n',
        )
        readme_text = readme_text.replace('R2.6.12 relay behavior', 'R2.6.12.1 relay behavior')
        readme_text = readme_text.replace('patch_version: 2.6.12', 'patch_version: 2.6.12.1')
        write_utf8(readme, readme_text)

        validation = patch_root / 'STATIC_VALIDATION.txt'
        write_utf8(
            validation,
            validation.read_text(encoding='utf-8-sig').replace(
                'R2.6.12 static validation contract',
                'R2.6.12.1 static validation contract',
            ),
        )

        stop = patch_root / 'payload/scripts/Stop-MiruStableMode.ps1'
        write_utf8(
            stop,
            stop.read_text(encoding='utf-8-sig').replace(
                'MIRU PC R2.6.12 STOPPED',
                'MIRU PC R2.6.12.1 STOPPED',
            ),
        )

        relay = patch_root / 'payload/root/Start-MiruContinuousSlidesRelay.ps1'
        write_utf8(
            relay,
            relay.read_text(encoding='utf-8-sig').replace(
                'MIRU-PC-Original-Resolution-Slides-Relay/2.6.12',
                'MIRU-PC-Original-Resolution-Slides-Relay/2.6.12.1',
            ),
        )

        manifest = patch_root / 'MANIFEST.sha256'
        manifest_lines = []
        for file_path in sorted(path for path in patch_root.rglob('*') if path.is_file() and path != manifest):
            relative = file_path.relative_to(patch_root).as_posix()
            manifest_lines.append(f'{sha256(file_path)} *{relative}')
        manifest.write_text('\n'.join(manifest_lines) + '\n', encoding='ascii', newline='\r\n')

        if (patch_root / 'VERSION.txt').read_text(encoding='utf-8').strip() != VERSION:
            raise RuntimeError('VERSION writeback failed')
        checked_static = static.read_text(encoding='utf-8')
        if ".Trim()-ne '2.6.12.1'" not in checked_static or ".Trim()-ne '2.6.12'" in checked_static:
            raise RuntimeError('static VERSION contract failed')
        policy = (patch_root / 'payload/config/miru-stable-policy.json').read_text(encoding='utf-8')
        if '"patch_version": "2.6.12.1"' not in policy:
            raise RuntimeError('policy version failed')
        installer = (patch_root / 'payload/scripts/Install-MiruPcStabilityPatch.ps1').read_text(encoding='utf-8')
        if "patch_version = '2.6.12.1'" not in installer:
            raise RuntimeError('installer version failed')

        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for file_path in sorted(path for path in final_root.rglob('*') if path.is_file()):
                archive.write(file_path, file_path.relative_to(final_root.parent).as_posix())
        output.with_suffix(output.suffix + '.sha256').write_text(
            f'{sha256(output)}  {output.name}\n',
            encoding='ascii',
        )
        print('MIRU_PC_R26121_RECOVERY_REPACK=PASS')
        print(f'SHA256={sha256(output)}')


if __name__ == '__main__':
    main()
