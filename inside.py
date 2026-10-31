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

def add_fixture_module(baseline):
    target = S / 'crates/within/src/operator/schwarz.rs'
    text = target.read_text()
    assert 'mod uniform_fixture_tests;' not in text
    target.write_text(text + '\n#[cfg(test)]\nmod uniform_fixture_tests;\n')
    source = (P / 'uniform_fixture_tests.rs').read_text()
    if baseline:
        source = source.split('#[test]\nfn uniform_fixture_deserializes_and_solves')[0]
    module = S / 'crates/within/src/operator/schwarz/uniform_fixture_tests.rs'
    assert module.parent.is_dir() and not module.exists()
    module.write_text(source)

result = {'phase': PHASE, 'status': 'STARTING', 'full_validation': False}
try:
    assert sys.version_info[:3] == (3, 12, 16)
    assert S.is_dir() and (R / 'home').is_dir()
    for file, expected in M['source_sha256'].items():
        assert hashlib.sha256((S / file).read_bytes()).hexdigest() == expected
    venv = prepare_venv()
    if PHASE == 'baseline':
        add_fixture_module(True)
        run('fetch-locked', ['cargo', 'fetch', '--locked'], 180)
        crates = json.loads((P / 'locked-crates.json').read_text())
        assert len(crates) == 222
        for crate in crates:
            archives = list((R / 'cargo/registry/cache').glob('*/' + crate['name'] + '-' + crate['version'] + '.crate'))
            assert len(archives) == 1, 'LOCKED_CRATE_MISSING_' + crate['name']
            assert hashlib.sha256(archives[0].read_bytes()).hexdigest() == crate['checksum'], 'LOCKED_CRATE_IDENTITY_' + crate['name']
        result['verified_locked_crates'] = len(crates)
        debug, _ = run('debug-calibration', ['cargo', 'test', '--workspace', '--all-features',
                                            '--all-targets', '--locked', '--no-run'], 720)
        tests, _ = run('rust-tests', ['cargo', 'test', '--workspace', '--all-features', '--locked'], 300)
        os.environ['WITHIN_UNIFORM_FIXTURE_VERSION'] = '20'
        run('generate-uniform-v20', ['cargo', 'test', '-p', 'within', '--lib',
                                    'uniform_fixture_tests::regenerate_uniform_partition_fixture',
                                    '--locked', '--', '--ignored', '--nocapture'], 60)
        fixture = S / 'crates/within/tests/fixtures/preconditioner_uniform_v20.postcard'
        shutil.copy2(fixture, R / 'preconditioner_uniform_v20.postcard')
        extension, _ = run('extension', ['maturin', 'develop', '--locked'], 360)
        python_tests, _ = run('python-tests', ['pytest', 'tests/', '-v'], 300)
        run('pickle-witness', ['python', str(P / 'pickle_demo.py'), 'baseline', str(R)], 30)
        result.update(status='CALIBRATION_COMPLETE', debug_seconds=debug,
                      tests_seconds=tests + python_tests, extension_seconds=extension,
                      uniform_fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest())
    else:
        base = json.loads((R / 'baseline-result.json').read_text())
        assert base['status'] == 'CALIBRATION_COMPLETE'
        projected = (sum(M['projection_factors'].values()) * base['debug_seconds']
                     + 2 * base['tests_seconds'] + M['projection_extra_seconds']) * M['projection_multiplier']
        assert time.time() + projected < END, 'FULL_FINISH_PROJECTION_NO_GO'
        result['projected_remaining_seconds'] = projected
        run('apply-core', ['git', 'apply', '--check', str(P / 'I1-core.patch')], 10)
        run('apply-core-once', ['git', 'apply', str(P / 'I1-core.patch')], 10)
        run('apply-wire-check', ['git', 'apply', '--check', str(P / 'I2-wire.patch')], 10)
        run('apply-wire-once', ['git', 'apply', str(P / 'I2-wire.patch')], 10)
        add_fixture_module(False)
        shutil.copy2(P / 'test_uniform_pickle.py', S / 'tests/test_uniform_pickle.py')
        changelog = S / 'CHANGELOG.md'
        text = changelog.read_text()
        unreleased = re.search(r'^## \[Unreleased\]\s*$', text, re.M)
        assert unreleased
        released = re.search(r'^## \[', text[unreleased.end():], re.M)
        assert released
        end_unreleased = unreleased.end() + released.start()
        changed = text.find('### Changed\n', unreleased.end(), end_unreleased)
        assert changed >= 0 and text.find('### Changed\n') == changed
        text = text.replace('### Changed\n', '### Changed\n\n'
            '- **BREAKING:** `schwarz_precond::PartitionWeights::Uniform` is a unit variant; '
            '`PartitionWeights::len` and `is_empty` are removed. Uniform weights take their '
            'shape from `SubdomainCore::n_local`; only explicit weights carry a length. '
            'The serialized uniform variant no longer carries a count; the wire-format fixtures move v20 → v21. New signed and uniform wire '
            'fixtures use v21; signed v20, v19 and v12 remain rejected-payload controls. '
            'The fixture number is not a runtime serialization envelope (#425).\n', 1)
        changelog.write_text(text)
        shutil.copy2(R / 'preconditioner_uniform_v20.postcard',
                     S / 'crates/within/tests/fixtures/preconditioner_uniform_v20.postcard')
        run('visible-test-module', ['git', 'add', '--intent-to-add',
            'crates/within/src/operator/schwarz/uniform_fixture_tests.rs',
            'tests/test_uniform_pickle.py',
            'crates/within/tests/fixtures/preconditioner_uniform_v20.postcard'], 10)
        os.environ['WITHIN_UNIFORM_FIXTURE_VERSION'] = '21'
        run('generate-uniform-v21', ['cargo', 'test', '-p', 'within', '--lib',
                                    'uniform_fixture_tests::regenerate_uniform_partition_fixture',
                                    '--locked', '--', '--ignored', '--nocapture'], 300)
        run('visible-candidate-fixture', ['git', 'add', '--intent-to-add',
            'crates/within/tests/fixtures/preconditioner_uniform_v21.postcard'], 10)
        _, uniform_output = run('uniform-compatibility', ['cargo', 'test', '-p', 'within', '--lib',
                                      'uniform_fixture_tests::', '--locked', '--', '--nocapture'], 90)
        outcomes = re.findall(rb'OLD_UNIFORM_OUTCOME=(REJECTED|COMPATIBLE)', uniform_output)
        assert len(outcomes) == 1
        result['old_uniform_outcome'] = outcomes[0].decode()
        assert not (S / 'crates/within/tests/fixtures/preconditioner_v21.postcard').exists(), 'SIGNED_V21_ALREADY_EXISTS'
        run('generate-signed-v21', ['cargo', 'test', '-p', 'within', '--test',
            'wire_format_fixture', 'regenerate_wire_format_fixture', '--locked',
            '--', '--ignored', '--nocapture'], 120)
        run('visible-signed-fixture', ['git', 'add', '--intent-to-add',
            'crates/within/tests/fixtures/preconditioner_v21.postcard'], 10)
        run('signed-compatibility', ['cargo', 'test', '-p', 'within', '--test',
            'wire_format_fixture', '--locked', '--', '--nocapture'], 90)
        result['fixture_sha256'] = {name: hashlib.sha256((S / 'crates/within/tests/fixtures' / name).read_bytes()).hexdigest()
            for name in ('preconditioner_v21.postcard', 'preconditioner_uniform_v21.postcard',
                         'preconditioner_v20.postcard', 'preconditioner_uniform_v20.postcard')} 
        run('rustfmt', ['cargo', 'fmt', '--all'], 45)
        run('rustfmt-check', ['cargo', 'fmt', '--all', '--', '--check'], 30)
        # Run the real project script unchanged, including its 94% floor.
        _, coverage_output = run('coverage', ['bash', 'scripts/coverage-with-python.sh'], 900)
        result['coverage_output_tail'] = coverage_output.decode(errors='replace')[-10000:]
        result['rust_test_result_lines'] = re.findall(r'^test result:.*$', coverage_output.decode(errors='replace'), re.M)
        _, pickle_output = run('pickle-witness', ['python', str(P / 'pickle_demo.py'), 'candidate', str(R)], 30)
        result['pickle_witness_output'] = pickle_output.decode(errors='replace')[-3000:]
        run('clippy', ['cargo', 'clippy', '--workspace', '--all-targets', '--all-features',
                       '--locked', '--', '-D', 'warnings'], 360)
        msrv = os.environ.copy()
        msrv['PATH'] = '/opt/toolchains/1.85.0/bin:' + msrv['PATH']
        msrv['CARGO_TARGET_DIR'] = str(R / 'target-msrv')
        run('msrv', ['cargo', 'check', '--workspace', '--all-features', '--all-targets', '--locked'],
            420, env=msrv)
        run('deny', ['cargo', 'deny', '--locked', 'check'], 180)
        advisory_repositories = list((R / 'cargo/advisory-dbs').glob('*/.git'))
        assert len(advisory_repositories) == 1, 'ADVISORY_DATABASE_IDENTITY_UNKNOWN'
        _, advisory_sha = run('advisory-database', ['git', '-C',
            str(advisory_repositories[0].parent), 'rev-parse', 'HEAD'], 10)
        result['advisory_database_commit'] = advisory_sha.decode().strip()
        assert re.fullmatch('[a-f0-9]{40}', result['advisory_database_commit'])
        run('semver', ['cargo', 'semver-checks', 'check-release', '--package', 'within',
                       '--package', 'schwarz-precond', '--baseline-version', '0.4.0'], 540)
        run('release-wheel', ['maturin', 'build', '--release', '--locked',
                              '--compatibility', 'linux', '--out', str(R / 'dist')], 480)
        run('sdist', ['maturin', 'sdist', '--out', str(R / 'dist')], 90)
        run('benchmark-smoke', ['cargo', 'bench', '-p', 'within', '--bench',
                                'store_backend', '--locked', '--', '--test'], 240)
        wheels = list((R / 'dist').glob('*.whl'))
        assert len(wheels) == 1
        installed = R / 'installed'
        run('installed-venv', ['python3', '-m', 'venv', str(installed)], 30)
        run('installed-wheel', [str(installed / 'bin/python'), '-m', 'pip', 'install',
                                '--no-index', '--no-deps', str(wheels[0]),
                                '/inputs/' + next(x['name'] for x in json.loads((P / 'artifacts.json').read_text())
                                                   if x['name'].startswith('numpy-'))], 60)
        run('installed-demo', [str(installed / 'bin/python'), str(P / 'pickle_demo.py'),
                               'candidate', str(R)], 30, cwd=R)
        for file in ('Cargo.lock', 'pixi.lock', 'pyproject.toml', 'scripts/coverage-with-python.sh',
                     'crates/within/tests/fixtures/preconditioner_v20.postcard'):
            assert hashlib.sha256((S / file).read_bytes()).hexdigest() == M['source_sha256'][file]
        run('visible-new-files', ['git', 'add', '--intent-to-add',
            'crates/within/src/operator/schwarz/uniform_fixture_tests.rs',
            'crates/within/tests/fixtures/preconditioner_uniform_v20.postcard',
            'crates/within/tests/fixtures/preconditioner_uniform_v21.postcard',
            'crates/within/tests/fixtures/preconditioner_v21.postcard',
            'tests/test_uniform_pickle.py'], 10)
        _, paths = run('final-path-allowlist', ['git', 'diff', '--name-only', 'HEAD'], 10)
        expected = {'crates/schwarz-precond/src/domain.rs',
                    'crates/within/src/operator/schwarz.rs',
                    'crates/within/src/operator/schwarz/uniform_fixture_tests.rs',
                    'crates/within/tests/fixtures/preconditioner_uniform_v20.postcard',
                    'crates/within/tests/fixtures/preconditioner_uniform_v21.postcard',
                    'crates/within/tests/wire_format_fixture.rs',
                    'crates/within/tests/fixtures/preconditioner_v21.postcard',
                    'tests/test_uniform_pickle.py', 'CHANGELOG.md'}
        assert set(paths.decode().splitlines()) == expected, 'FINAL_PATH_ALLOWLIST'
        _, patch = run('final-patch', ['git', 'diff', '--binary', 'HEAD'], 10)
        (R / 'candidate-final.patch').write_bytes(patch)
        result.update(status='FULL_VALIDATION_PASS', full_validation=True,
                      patch_sha256=hashlib.sha256(patch).hexdigest(),
                      limitations=['Native Linux ARM64/Python3.12 only; not upstream Pixi x64/3.14t or Mac/Windows',
                                   'Local semver toolchain pinned1.97; upstream stable job remains separate',
                                   'Native Linux wheel; full release-wheel matrix/abi3audit not run'])
except BaseException as error:
    result.update(status='STOP_' + type(error).__name__, error=str(error))
    raise
finally:
    result['steps'] = LOGS
    result['runtime_disk'] = dict(zip(('total', 'used', 'free'), shutil.disk_usage(R)))
    result['cgroup'] = {}
    for name in ('memory.peak', 'memory.events', 'memory.current', 'memory.stat'):
        path = Path('/sys/fs/cgroup') / name
        if path.is_file():
            result['cgroup'][name] = path.read_text()
    result['memory_measurement_note'] = 'Raw kernel high-water includes cache; working-set samples are in the host record, not a measured continuous peak.'
    if PHASE == 'candidate' and (R / 'baseline-result.json').is_file():
        calibration = json.loads((R / 'baseline-result.json').read_text())
        reference = calibration.get('debug_seconds', 0)
        result['projection_label_note'] = 'python_extension factor is reserve for candidate test-module build; rebuilt extension is included in actual coverage duration.'
        groups = {'coverage': ('coverage',), 'clippy': ('clippy',), 'msrv': ('msrv',),
                  'python_extension': ('generate-uniform-v21',),
                  'release': ('release-wheel', 'sdist', 'benchmark-smoke'), 'semver': ('semver',)}
        result['actual_vs_projected'] = {key: {
            'declared_factor': M['projection_factors'][key],
            'actual_seconds': sum(step['elapsed'] for step in LOGS if step['label'] in labels),
            'actual_debug_multiple': sum(step['elapsed'] for step in LOGS if step['label'] in labels) / reference if reference else None
        } for key, labels in groups.items()}
    (R / (PHASE + '-result.json')).write_text(json.dumps(result, indent=2))
    failures = [x for x in LOGS if x['exit'] != 0]
    for step in (failures + [x for x in LOGS if x['exit'] == 0])[:2]:
        print((R / (PHASE + '-' + step['label'] + '.log')).read_text(errors='replace')[-12000:], flush=True)
    print('WITHIN_RESULT=' + json.dumps(result), flush=True)
