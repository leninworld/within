"""Bounded hosted Docker orchestration, adapted from the verified Magpie runner."""
from pathlib import Path
import base64
import hashlib
import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import time
import urllib.parse
import urllib.request

P = Path(__file__).resolve().parent
SOURCE = P.parent / 'source'
R = P.parent / 'runtime'
M = json.loads((P / 'manifest.json').read_text())
START_EPOCH = float(os.environ['VALIDATION_JOB_STARTED_EPOCH'])
END = START_EPOCH + M['target_seconds']
OWN = None
LAST_SAMPLE = 0
PHASE = 'setup'
LOGS = []
SAMPLES = []
RESULT = {'status': 'PREPARING', 'full_validation': False, 'job_start': START_EPOCH,
          'target_epoch': END, 'absolute_epoch': START_EPOCH + M['absolute_seconds']}
routes = [line.split()[0] for line in Path('/proc/net/route').read_text().splitlines()[1:]
          if line.split()[1] == '00000000']
assert len(set(routes)) == 1
RX = Path('/sys/class/net') / routes[0] / 'statistics/rx_bytes'
NETWORK_START = int(RX.read_text())

def guard():
    assert time.time() < END, 'JOB_TARGET_DEADLINE'
    assert shutil.disk_usage(P).free >= 4 * 1024**3, 'HOST_DISK_PRESSURE'
    available = int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1]) * 1024
    assert available >= 1536 * 1024**2, 'HOST_MEMORY_PRESSURE'
    assert int(RX.read_text()) - NETWORK_START < 4 * 1024**3, 'RUNTIME_NETWORK_CAP'

def command(label, args, seconds, cap=4 * 1024**2, monitor=True):
    global LAST_SAMPLE
    guard()
    assert re.fullmatch('[a-z0-9-]+', label)
    target = R / (label + '.log')
    assert not target.exists()
    env = {k: v for k, v in os.environ.items()
           if not any(part in k.upper() for part in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY'))}
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks = []
    size = 0
    started = time.monotonic()
    error = None
    try:
        while selector.get_map():
            guard()
            assert time.monotonic() - started < seconds, 'STEP_TIME_' + label
            if monitor and OWN and time.monotonic() - LAST_SAMPLE >= 5:
                LAST_SAMPLE = time.monotonic()
                state = json.loads(command('state-' + str(len(LOGS)),
                    ['docker', 'inspect', '--format', '{{json .State}}', OWN], 8, 8192, False))
                assert not state['OOMKilled'], 'CONTAINER_OOM'
                if state['Running']:
                    raw = command('memory-' + str(len(LOGS)),
                        ['docker', 'stats', '--no-stream', '--format', '{{.MemPerc}}', OWN],
                        8, 8192, False).decode().strip()
                    assert re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%', raw), 'MISSING_WORKING_SET_SAMPLE'
                    percent = float(raw[:-1])
                    limit = int(command('limit-' + str(len(LOGS)),
                        ['docker', 'inspect', '--format', '{{.HostConfig.Memory}}', OWN],
                        8, 8192, False))
                    sample = {'at': time.time(), 'phase': PHASE, 'percent': percent,
                              'limit': limit, 'sampled_working_set_upper_bytes':
                              math.ceil((percent + .01) / 100 * limit) + 1024**2}
                    SAMPLES.append(sample)
                    assert percent < 93.75, 'CONTAINER_WORKING_SET_PRESSURE'
            for key, _ in selector.select(.25):
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                assert size + len(data) <= cap, 'OUTPUT_CAP_' + label
                chunks.append(data)
                size += len(data)
        assert process.wait(timeout=3) == 0, 'STEP_FAILED_' + label
        return b''.join(chunks)
    except BaseException as exc:
        error = str(exc)
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
        target.write_bytes(b''.join(chunks))
        LOGS.append({'label': label, 'args': args, 'exit': process.returncode,
                     'elapsed': time.monotonic() - started, 'bytes': size, 'error': error})

def download(item):
    allowed = {'files.pythonhosted.org', 'static.rust-lang.org', 'github.com',
               'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, newurl):
            parsed = urllib.parse.urlsplit(newurl)
            assert parsed.scheme == 'https' and parsed.hostname in allowed
            return super().redirect_request(request, response, code, message, headers, newurl)
    parsed = urllib.parse.urlsplit(item['url'])
    assert parsed.scheme == 'https' and parsed.hostname in allowed
    target = R / 'inputs' / item['name']
    assert not target.exists()
    size = 0
    digest = hashlib.sha256()
    started = time.monotonic()
    with urllib.request.build_opener(Redirect()).open(item['url'], timeout=20) as source, target.open('xb') as output:
        assert source.status == 200
        while True:
            guard()
            assert time.monotonic() - started < 120, 'INPUT_TIME_CAP'
            data = source.read(65536)
            if not data:
                break
            assert size + len(data) <= item['bytes'], 'INPUT_SIZE_CAP'
            output.write(data)
            size += len(data)
            digest.update(data)
    assert size == item['bytes'] and digest.hexdigest() == item['sha256'], 'INPUT_IDENTITY'

def run_phase(phase, memory_gib):
    global OWN, PHASE
    PHASE = phase
    ident = os.environ['GITHUB_RUN_ID']
    name = 'within425-' + phase + '-' + ident
    args = ['docker', 'create', '--name', name, '--label', 'org.within425.run=' + ident,
            '--init', '--read-only', '--cpus', '3', '--memory', str(memory_gib) + 'g',
            '--memory-swap', str(memory_gib) + 'g', '--pids-limit', '512',
            '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL',
            '--env', 'VALIDATION_DEADLINE_EPOCH=' + str(END),
            '--env', 'TMPDIR=/runtime/tmp',
            '--mount', 'type=bind,src=' + str(P) + ',dst=/packet,readonly',
            '--mount', 'type=bind,src=' + str(R) + ',dst=/runtime',
            '--mount', 'type=bind,src=' + str(R / 'inputs') + ',dst=/inputs,readonly',
            'within425-validation:' + ident, 'python3', '/packet/inside.py', phase]
    OWN = command(phase + '-create', args, 15).decode().strip()
    assert re.fullmatch('[a-f0-9]{64}', OWN)
    command(phase + '-run', ['docker', 'start', '--attach', OWN], 45 * 60)
    state = json.loads(command(phase + '-state',
        ['docker', 'inspect', '--format', '{{json .State}}', OWN], 10, 8192, False))
    assert not state['Running'] and not state['OOMKilled'] and state['ExitCode'] == 0
    OWN = None
    return json.loads((R / (phase + '-result.json')).read_text())

try:
    assert platform.machine() == 'aarch64' and os.getuid() == 1001
    assert 0 <= time.time() - START_EPOCH < 300, 'STALE_JOB_START'
    R.mkdir()
    for directory in ('inputs', 'home', 'tmp'):
        (R / directory).mkdir()
    assert shutil.disk_usage(P).free >= 36 * 1024**3, 'WORKSPACE_DISK_ADMISSION'
    info = json.loads(command('docker-info', ['docker', 'info', '--format',
        '{"root":{{json .DockerRootDir}},"memory":{{.MemTotal}},"cpus":{{.NCPU}}}'], 15))
    available = int(re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)[1]) * 1024
    assert info['cpus'] >= 4 and info['memory'] >= 15 * 1024**3 and available >= 14 * 1024**3
    assert shutil.disk_usage(info['root']).free >= 36 * 1024**3
    RESULT['admission'] = {'available': available, 'workspace_free': shutil.disk_usage(P).free,
                            'docker_free': shutil.disk_usage(info['root']).free, 'docker': info}
    assert command('source-clean', ['git', '-C', str(SOURCE), 'status', '--porcelain'], 10) == b''
    assert command('source-base', ['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], 10).decode().strip() == M['base']
    for path, expected in M['source_sha256'].items():
        assert hashlib.sha256((SOURCE / path).read_bytes()).hexdigest() == expected
    auth = subprocess.run(['git', '-C', str(SOURCE), 'config', '--local', '--get-regexp',
                           'extraheader|credential'], capture_output=True, timeout=5)
    assert auth.returncode == 1 and auth.stdout == b'', 'SOURCE_CREDENTIAL_CONFIG'
    for phase in ('candidate',):
        shutil.copytree(SOURCE, R / phase)
    for index, image in enumerate((M['python_image'], M['rust_image'])):
        command('pull-' + str(index), ['docker', 'pull', '--platform', 'linux/arm64', image], 300)
        metadata = json.loads(command('image-' + str(index), ['docker', 'image', 'inspect', image], 10, 65536))[0]
        assert metadata['Architecture'] == 'arm64' and image in metadata['RepoDigests']
        assert metadata['Size'] <= 4 * 1024**3, 'IMAGE_EXPANSION_CAP'
    artifacts = json.loads((P / 'artifacts.json').read_text())
    assert len(artifacts) == 24 and sum(x['bytes'] for x in artifacts) == M['artifact_compressed_bytes']
    for item in artifacts:
        download(item)
    ident = os.environ['GITHUB_RUN_ID']
    assert re.fullmatch('[0-9]+', ident)
    command('build-tools-image', ['docker', 'build', '--network=none',
        '--build-context', 'verified_inputs=' + str(R / 'inputs'),
        '--tag', 'within425-validation:' + ident, '--file', str(P / 'Dockerfile'), str(P)], 300)
    assert time.time() - START_EPOCH < 12 * 60, 'COLD_SETUP_TIME_CAP'
    candidate = run_phase('candidate', 12)
    assert candidate['status'] == 'FULL_VALIDATION_PASS' and candidate['full_validation']
    patch = (R / 'candidate-final.patch').read_bytes()
    assert len(patch) <= 256 * 1024
    RESULT.update(status='FULL_VALIDATION_PASS', full_validation=True, candidate=candidate)
    print('FINAL_PATCH_BASE64=' + base64.b64encode(patch).decode(), flush=True)
except BaseException as error:
    RESULT.update(status='STOP_' + type(error).__name__, error=str(error))
finally:
    if OWN:
        state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', OWN],
                               capture_output=True, timeout=10)
        if state.returncode == 0 and json.loads(state.stdout)['Running']:
            subprocess.run(['docker', 'stop', '--time', '2', OWN], capture_output=True, timeout=10)
        check = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', OWN],
                               capture_output=True, timeout=10)
        RESULT['owned_stop_verified'] = check.returncode == 0 and not json.loads(check.stdout)['Running']
    else:
        RESULT['owned_stop_verified'] = True
    RESULT['host_steps'] = LOGS
    RESULT['runtime_network_received_bytes'] = int(RX.read_text()) - NETWORK_START
    if R.exists():
        (R / 'host-result.json').write_text(json.dumps(RESULT, indent=2))
        sample_text = ''.join(json.dumps(x) + '\n' for x in SAMPLES)
        assert len(sample_text.encode()) <= 256 * 1024, 'MEMORY_EVIDENCE_CAP'
        (R / 'memory-samples.jsonl').write_text(sample_text)
        print('MEMORY_SAMPLES_JSONL_BEGIN\n' + sample_text + 'MEMORY_SAMPLES_JSONL_END', flush=True)
        for step in [x for x in LOGS if x['exit'] != 0][:1]:
            print((R / (step['label'] + '.log')).read_text(errors='replace')[-16000:], flush=True)
    print('WITHIN_HOST_RESULT=' + json.dumps(RESULT), flush=True)
assert RESULT['full_validation'] and RESULT['owned_stop_verified']
