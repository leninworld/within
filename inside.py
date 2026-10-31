"""Pinned Ruff style checks only, in a network-isolated Docker container."""
from pathlib import Path
import hashlib,json,os,subprocess,time,selectors,signal
P=Path('/packet');R=Path('/runtime');S=Path('/source')
M=json.loads((P/'manifest.json').read_text());end=float(os.environ['STYLE_DEADLINE_EPOCH'])
assert os.getuid()==1001
for path,sha in M['candidate_sha256'].items():
 assert hashlib.sha256((S/path).read_bytes()).hexdigest()==sha
results=[]
def run(args,timeout=30):
 assert time.time()<end
 proc=subprocess.Popen(args,cwd=S,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
 sel=selectors.DefaultSelector();sel.register(proc.stdout,selectors.EVENT_READ,'stdout');sel.register(proc.stderr,selectors.EVENT_READ,'stderr')
 chunks={'stdout':[],'stderr':[]};size=0;start=time.monotonic()
 try:
  while sel.get_map():
   assert time.time()<end and time.monotonic()-start<timeout,'STYLE_COMMAND_TIMEOUT'
   for key,_ in sel.select(.1):
    part=os.read(key.fd,8192)
    if not part:sel.unregister(key.fileobj);continue
    size+=len(part);assert size<1024*1024,'STYLE_OUTPUT_CAP';chunks[key.data].append(part)
  proc.wait(timeout=2)
 finally:
  sel.close()
  if proc.poll() is None:
   os.killpg(proc.pid,signal.SIGTERM)
   try:proc.wait(timeout=2)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
  item={'args':args,'exit':proc.returncode,**{k:b''.join(v).decode(errors='replace') for k,v in chunks.items()}};results.append(item)
  print(item['stdout'],end='',flush=True);print(item['stderr'],end='',flush=True)
  print(json.dumps(item),flush=True)
 assert proc.returncode==0
run(['python3','-m','venv',str(R/'venv')])
pip=str(R/'venv/bin/pip');ruff=str(R/'venv/bin/ruff')
run([pip,'install','--no-index','--no-deps','--no-cache-dir','/inputs/'+M['wheel']['name']])
run([ruff,'--version'])
assert results[-1]['stdout'].strip()=='ruff 0.9.10'
files=['tests/test_error_handling.py','tests/test_preconditioner.py']
run([ruff,'check',*files])
run([ruff,'format','--check',*files])
for path,sha in M['candidate_sha256'].items():
 assert hashlib.sha256((S/path).read_bytes()).hexdigest()==sha
print('STYLE_RESULT='+json.dumps({'status':'PASS','revision':M['base'],'patch_sha256':M['patch_sha256'],
 'candidate_sha256':M['candidate_sha256'],'checks':results,'no_source_mutation':True}),flush=True)
