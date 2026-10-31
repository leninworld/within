"""Build-time installer for the declared official inputs; Docker only."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile

assert os.getuid() == 0
assert sys.version_info[:3] == (3, 12, 16)
artifacts = json.loads(Path('/packet/artifacts.json').read_text())
assert len(artifacts) == 24
for number, item in enumerate(artifacts):
    archive = Path('/inputs') / item['name']
    assert archive.stat().st_size == item['bytes']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == item['sha256']
    if item['kind'] == 'wheel':
        continue
    directory = Path('/opt/stage') / str(number)
    directory.mkdir(parents=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and '..' not in path.parts
            assert member.isfile() or member.isdir()
            assert not member.mode & 0o6000
        assert sum(x.size for x in members if x.isfile()) == item['unpacked_bytes']
        source.extractall(directory, filter='data')
    if item['name'].startswith('rust-'):
        installers = list(directory.glob('*/install.sh'))
        assert len(installers) == 1
        version = '1.97.0' if item['name'].startswith('rust-1.97.0-') else '1.85.0'
        subprocess.run(['bash', str(installers[0]), '--disable-ldconfig',
                        '--prefix=/opt/toolchains/' + version],
                       cwd=installers[0].parent, check=True, timeout=180)
    else:
        name = ('cargo-llvm-cov' if item['name'].startswith('cargo-llvm-cov') else
                'cargo-deny' if item['name'].startswith('cargo-deny') else 'cargo-semver-checks')
        matches = list(directory.rglob(name))
        assert len(matches) == 1 and matches[0].is_file()
        data = matches[0].read_bytes()
        assert data[:4] == b'\x7fELF' and struct.unpack_from('<H', data, 18)[0] == 183
        Path('/opt/tools').mkdir(exist_ok=True)
        shutil.copy2(matches[0], Path('/opt/tools') / name)
        (Path('/opt/tools') / name).chmod(0o755)
for command in ('cc', 'ld', 'git'):
    subprocess.run([command, '--version'], check=True, timeout=10)
subprocess.run(['ldd', '/usr/local/bin/python3.12'], check=True, timeout=10)
import ssl, sqlite3, lzma, bz2, ctypes, sysconfig
assert sys.prefix == '/usr/local' and sysconfig.get_config_var('LDLIBRARY') == 'libpython3.12.so'
print('OFFICIAL_INPUT_INSTALL_COMPLETE', flush=True)
