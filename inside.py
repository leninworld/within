"""One full, deadline-bound baseline/candidate route. Never run on the Mac."""
from pathlib import Path
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

P = Path('/packet')
R = Path('/runtime')
M = json.loads((P / 'manifest.json').read_text())
END = float(os.environ['VALIDATION_DEADLINE_EPOCH'])
PHASE = sys.argv[1]
assert PHASE in ('baseline', 'candidate') and os.getuid() == 1001
S = R / PHASE
LOGS = []

def guard():
    assert time.time() < END, 'ORIGINAL_TARGET_DEADLINE'
    assert shutil.disk_usage(R).free >= 4 * 1024**3, 'DISK_PRESSURE'

def run(label, args, seconds, cwd=S, env=None, cap=4 * 1024**2):
    guard()
    assert re.fullmatch('[a-z0-9-]+', label)
    destination = R / (PHASE + '-' + label + '.log')
    assert not destination.exists()
    process = subprocess.Popen(args, cwd=cwd, env=env or os.environ.copy(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    started = time.monotonic()
    size = 0
    chunks = []
    try:
        while selector.get_map():
            guard()
            assert time.monotonic() - started < seconds, 'STEP_TIME_' + label
            for key, _ in selector.select(.25):
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                assert size + len(data) <= cap, 'OUTPUT_CAP_' + label
                size += len(data)
                chunks.append(data)
        code = process.wait(timeout=3)
        assert code == 0, 'STEP_FAILED_' + label
        return time.monotonic() - started, b''.join(chunks)
    except BaseException:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        raise
    finally:
        selector.close()
        destination.write_bytes(b''.join(chunks))
        LOGS.append({'label': label, 'args': args, 'exit': process.returncode,
                     'elapsed': time.monotonic() - started, 'bytes': size})

def prepare_venv():
    venv = R / ('venv-' + PHASE)
    run('venv', ['python3', '-m', 'venv', str(venv)], 30)
    run('wheels', [str(venv / 'bin/python'), '-m', 'pip', 'install', '--no-index',
                  '--no-deps', '--require-hashes', '--find-links=/inputs',
                  '-r', str(P / 'wheels.lock')], 90)
    os.environ.update(PATH=str(venv / 'bin') + ':' + os.environ['PATH'],
                      VIRTUAL_ENV=str(venv), CARGO_HOME=str(R / 'cargo'),
                      CARGO_TARGET_DIR=str(R / 'target-debug'), CARGO_BUILD_JOBS='3',
                      RAYON_NUM_THREADS='3', HOME=str(R / 'home'),
                      LLVM_COV='/opt/toolchains/1.97.0/lib/rustlib/aarch64-unknown-linux-gnu/bin/llvm-cov',
                      LLVM_PROFDATA='/opt/toolchains/1.97.0/lib/rustlib/aarch64-unknown-linux-gnu/bin/llvm-profdata')
    for key in ('RUSTC_WRAPPER', 'RUSTUP_TOOLCHAIN', 'PYTHONPATH', 'PYTHONHOME'):
        os.environ.pop(key, None)
    run('tool-versions', ['bash', '-euc',
        'python -V\ncargo -V\nrustc -Vv\ncargo llvm-cov --version\n'
        'cargo deny --version\ncargo semver-checks --version\n'
        'python -m pip check\n'
        'cargo semver-checks check-release --help\n'
        'cargo deny check --help'], 30)
    return venv

result = {'phase': PHASE, 'status': 'PREPARING', 'full_validation': False}
try:
    assert PHASE == 'candidate' and S.is_dir()
    for file, expected in {**M['source_sha256'], **M['preserve_sha256']}.items():
        assert hashlib.sha256((S / file).read_bytes()).hexdigest() == expected
    assert hashlib.sha256((P / 'cleanup.patch').read_bytes()).hexdigest() == M['cleanup_patch_sha256']
    run('apply-check', ['git', 'apply', '--check', str(P / 'cleanup.patch')], 10)
    run('apply-cleanup', ['git', 'apply', str(P / 'cleanup.patch')], 10)
    for file, expected in M['delete_sha256'].items():
        assert hashlib.sha256((S / file).read_bytes()).hexdigest() == expected
        (S / file).unlink()
    prepare_venv()
    run('fetch-locked', ['cargo', 'fetch', '--locked'], 180)
    crates = json.loads((P / 'locked-crates.json').read_text())
    assert len(crates) == 222
    for crate in crates:
        archives = list((R / 'cargo/registry/cache').glob('*/' + crate['name'] + '-' + crate['version'] + '.crate'))
        assert len(archives) == 1
        assert hashlib.sha256(archives[0].read_bytes()).hexdigest() == crate['checksum']
    run('rustfmt-check', ['cargo', 'fmt', '--all', '--', '--check'], 30)
    _, coverage = run('coverage', ['bash', 'scripts/coverage-with-python.sh'], 1200)
    output = coverage.decode(errors='replace')
    required = ['wire_format_fixture_deserializes_and_solves',
                'preconditioner_round_trips::case_1_signed',
                'preconditioner_round_trips::case_2_uniform',
                'legacy_fixture_no_longer_decodes::case_1_v20',
                'legacy_fixture_no_longer_decodes::case_2_v19',
                'legacy_fixture_no_longer_decodes::case_3_release_0_3_0',
                'signed_wire_fixture_contains_uniform_weights']
    for name in required:
        assert re.search(re.escape(name) + r' \.\.\. ok', output), 'MISSING_TEST_' + name
    result['required_tests_passed'] = required
    result['coverage_output_tail'] = output[-12000:]
    result['rust_test_result_lines'] = re.findall(r'^test result:.*$', output, re.M)
    run('clippy', ['cargo', 'clippy', '--workspace', '--all-targets', '--all-features', '--locked', '--', '-D', 'warnings'], 360)
    msrv = os.environ.copy()
    msrv['PATH'] = '/opt/toolchains/1.85.0/bin:' + msrv['PATH']
    msrv['CARGO_TARGET_DIR'] = str(R / 'target-msrv')
    run('msrv', ['cargo', 'check', '--workspace', '--all-features', '--all-targets', '--locked'], 420, env=msrv)
    for file, expected in {**M['source_sha256'], **M['preserve_sha256']}.items():
        if file == 'crates/within/tests/wire_format_fixture.rs':
            continue
        assert hashlib.sha256((S / file).read_bytes()).hexdigest() == expected
    for file, expected in M['proposed_sha256'].items():
        assert hashlib.sha256((S / file).read_bytes()).hexdigest() == expected, 'PROPOSED_FILE_CHANGED_' + file
    _, paths = run('final-path-allowlist', ['git', 'diff', '--name-only', 'HEAD'], 10)
    assert set(paths.decode().splitlines()) == set(M['expected_paths'])
    _, patch = run('final-patch', ['git', 'diff', '--binary', 'HEAD'], 10)
    (R / 'candidate-final.patch').write_bytes(patch)
    result.update(status='FULL_VALIDATION_PASS', full_validation=True, patch_sha256=hashlib.sha256(patch).hexdigest())
except BaseException as error:
    result.update(status='STOP_' + type(error).__name__, error=str(error))
    raise
finally:
    result['steps'] = LOGS
    result['runtime_disk'] = dict(zip(('total', 'used', 'free'), shutil.disk_usage(R)))
    result['cgroup'] = {name: (Path('/sys/fs/cgroup') / name).read_text() for name in ('memory.peak', 'memory.events') if (Path('/sys/fs/cgroup') / name).is_file()}
    (R / (PHASE + '-result.json')).write_text(json.dumps(result, indent=2))
    for step in [x for x in LOGS if x['exit'] != 0][:1]:
        print((R / (PHASE + '-' + step['label'] + '.log')).read_text(errors='replace')[-16000:], flush=True)
    print('WITHIN_RESULT=' + json.dumps(result), flush=True)
