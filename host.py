"""One capped supplemental style-only Docker job; never run on the Mac."""
from pathlib import Path
import hashlib,json,os,platform,subprocess,time,urllib.request,zipfile,stat,selectors,signal
P=Path(__file__).resolve().parent;S=P.parent/'source';R=P.parent/'runtime'
M=json.loads((P/'manifest.json').read_text());START=float(os.environ['STYLE_JOB_STARTED_EPOCH']);END=START+180
assert platform.machine()=='aarch64' and os.getuid()==1001
assert time.time()-START<60
R.mkdir();(R/'tmp').mkdir();(R/'home').mkdir();(R/'inputs').mkdir()
assert os.statvfs(R).f_bavail*os.statvfs(R).f_frsize>2*1024**3
logs=[];owned=None;result={'status':'STARTING','full_product_validation':False}
def run(label,args,timeout):
 assert time.time()<END
 env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ['TOKEN','SECRET','PASSWORD','CREDENTIAL','API_KEY'])}
 proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
 sel=selectors.DefaultSelector();sel.register(proc.stdout,selectors.EVENT_READ,'stdout');sel.register(proc.stderr,selectors.EVENT_READ,'stderr')
 chunks={'stdout':[],'stderr':[]};size=0;start=time.monotonic()
 try:
  while sel.get_map():
   assert time.time()<END and time.monotonic()-start<timeout,'COMMAND_TIMEOUT_'+label
   for key,_ in sel.select(.1):
    part=os.read(key.fd,8192)
    if not part:sel.unregister(key.fileobj);continue
    size+=len(part);assert size<2*1024**2,'COMMAND_OUTPUT_CAP_'+label;chunks[key.data].append(part)
  proc.wait(timeout=2)
 finally:
  sel.close()
  if proc.poll() is None:
   os.killpg(proc.pid,signal.SIGTERM)
   try:proc.wait(timeout=2)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
  item={'label':label,'args':args,'exit':proc.returncode,**{k:b''.join(v).decode(errors='replace') for k,v in chunks.items()}};logs.append(item)
  print(item['stdout'],end='',flush=True);print(item['stderr'],end='',flush=True)
  print(json.dumps(item),flush=True)
 assert proc.returncode==0,label
 return item['stdout']

try:
 assert run('source-base',['git','-C',str(S),'rev-parse','HEAD'],10).strip()==M['base']
 assert run('source-clean',['git','-C',str(S),'status','--porcelain'],10)==''
 patch=(P/'publishable.patch').read_bytes();assert hashlib.sha256(patch).hexdigest()==M['patch_sha256']
 run('apply-check',['git','-C',str(S),'apply','--check',str(P/'publishable.patch')],10)
 run('apply',['git','-C',str(S),'apply',str(P/'publishable.patch')],10)
 for path,sha in M['candidate_sha256'].items():assert hashlib.sha256((S/path).read_bytes()).hexdigest()==sha
 w=M['wheel'];url=w['url'];assert url.startswith('https://files.pythonhosted.org/') and w['bytes']<20*1024**2
 dest=R/'inputs'/w['name'];size=0;h=hashlib.sha256();download_start=time.monotonic()
 with urllib.request.urlopen(url,timeout=20) as response,dest.open('xb') as out:
  assert response.status==200 and response.url.startswith('https://files.pythonhosted.org/')
  while True:
   assert time.time()<END and time.monotonic()-download_start<30
   b=response.read(65536)
   if not b:break
   size+=len(b);assert size<=w['bytes'];h.update(b);out.write(b)
 assert size==w['bytes'] and h.hexdigest()==w['sha256']
 with zipfile.ZipFile(dest) as z:
  entries=z.infolist();assert len(entries)<1000 and sum(x.file_size for x in entries)<128*1024**2
  assert all(not x.filename.startswith('/') and '..' not in Path(x.filename).parts and not stat.S_ISLNK(x.external_attr>>16) for x in entries)
 run('pull',['docker','pull','--platform','linux/arm64',M['image']],60)
 info=json.loads(run('image',['docker','image','inspect',M['image']],10))[0];assert info['Architecture']=='arm64' and M['image'] in info['RepoDigests']
 name='within426-ruff-'+os.environ['GITHUB_RUN_ID']
 args=['docker','create','--name',name,'--init','--read-only','--cpus','1','--memory','512m','--memory-swap','512m','--pids-limit','64','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges','--user','1001:1001','--env','STYLE_DEADLINE_EPOCH='+str(END),'--env','TMPDIR=/runtime/tmp','--env','RUFF_CACHE_DIR=/runtime/ruff-cache','--mount','type=bind,src='+str(P)+',dst=/packet,readonly','--mount','type=bind,src='+str(S)+',dst=/source,readonly','--mount','type=bind,src='+str(R)+',dst=/runtime','--mount','type=bind,src='+str(R/'inputs')+',dst=/inputs,readonly',M['image'],'python3','/packet/inside.py']
 owned=run('create',args,10).strip();output=run('style',['docker','start','--attach',owned],90)
 state=json.loads(run('state',['docker','inspect','--format','{{json .State}}',owned],10));assert not state['Running'] and not state['OOMKilled'] and state['ExitCode']==0
 lines=[x for x in output.splitlines() if x.startswith('STYLE_RESULT=')];assert len(lines)==1
 result=json.loads(lines[0].split('=',1)[1]);assert result['status']=='PASS'
except BaseException as error:
 result.update(status='STOP_'+type(error).__name__,error=str(error))
 raise
finally:
 if owned:
  state=subprocess.run(['docker','inspect','--format','{{json .State}}',owned],capture_output=True,text=True,timeout=10)
  if state.returncode==0 and json.loads(state.stdout)['Running']:subprocess.run(['docker','stop','--time','2',owned],capture_output=True,timeout=10)
  state=subprocess.run(['docker','inspect','--format','{{json .State}}',owned],capture_output=True,text=True,timeout=10)
  result['owned_stop_verified']=state.returncode==0 and not json.loads(state.stdout)['Running']
 else:result['owned_stop_verified']=True
 result['logs']=logs;result['elapsed']=time.time()-START
 print('RUFF_HOST_RESULT='+json.dumps(result),flush=True)
assert result['status']=='PASS' and result['owned_stop_verified']
