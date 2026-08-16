from __future__ import annotations
import base64, csv, hashlib, io, os, tarfile, zipfile
from pathlib import Path

NAME='effectfence'
VERSION='0.1.0'
DIST=f'{NAME}-{VERSION}'
WHEEL=f'{DIST}-py3-none-any.whl'

def _metadata():
    return (f'Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n'
            'Summary: Crash/retry side-effect safety verifier\n'
            'Requires-Python: >=3.10\nLicense: Apache-2.0\n\n')

def _wheel():
    return 'Wheel-Version: 1.0\nGenerator: effectfence-build-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n'

def _hash(data: bytes):
    dig=base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
    return f'sha256={dig}'

def _files(root: Path):
    for p in sorted((root/'effectfence').rglob('*.py')):
        yield p, p.relative_to(root).as_posix()

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    root=Path(__file__).resolve().parent
    out=Path(wheel_directory); out.mkdir(parents=True,exist_ok=True)
    target=out/WHEEL
    dist_info=f'{DIST}.dist-info'
    entries=[]
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for p,arc in _files(root):
            data=p.read_bytes(); z.writestr(arc,data); entries.append((arc,_hash(data),str(len(data))))
        meta=_metadata().encode(); arc=f'{dist_info}/METADATA'; z.writestr(arc,meta); entries.append((arc,_hash(meta),str(len(meta))))
        wh=_wheel().encode(); arc=f'{dist_info}/WHEEL'; z.writestr(arc,wh); entries.append((arc,_hash(wh),str(len(wh))))
        ep=b'[console_scripts]\neffectfence = effectfence.cli:main\n'; arc=f'{dist_info}/entry_points.txt'; z.writestr(arc,ep); entries.append((arc,_hash(ep),str(len(ep))))
        rec_arc=f'{dist_info}/RECORD'
        buf=io.StringIO(); w=csv.writer(buf,lineterminator='\n')
        for row in entries: w.writerow(row)
        w.writerow((rec_arc,'',''))
        z.writestr(rec_arc,buf.getvalue().encode())
    return WHEEL

def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    d=Path(metadata_directory)/f'{DIST}.dist-info'; d.mkdir(parents=True,exist_ok=True)
    (d/'METADATA').write_text(_metadata())
    (d/'WHEEL').write_text(_wheel())
    (d/'entry_points.txt').write_text('[console_scripts]\neffectfence = effectfence.cli:main\n')
    return d.name

def build_sdist(sdist_directory, config_settings=None):
    root=Path(__file__).resolve().parent
    out=Path(sdist_directory); out.mkdir(parents=True,exist_ok=True)
    name=f'{DIST}.tar.gz'; target=out/name
    excluded={'.git','.venv','__pycache__','out','dist','build'}
    with tarfile.open(target,'w:gz') as t:
        for p in sorted(root.rglob('*')):
            if not p.is_file() or any(x in excluded for x in p.parts): continue
            t.add(p,arcname=f'{DIST}/{p.relative_to(root)}')
    return name
