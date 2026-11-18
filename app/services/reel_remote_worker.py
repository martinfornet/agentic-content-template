"""Outbound-only Mac preview engine. No publication, no paid provider calls."""
from pathlib import Path
import urllib.parse
import argparse,base64,copy,hashlib,json,os,shutil,threading,time,urllib.request,urllib.error
from app.services.editorial_workflow import EditorialWorkflow
from app.services.reel_pipeline import ReelPipeline
from app.services.reel_story import place_packet,fingerprint,review_story
from app.services.reel_story_audio import audition

class Client:
 def __init__(self,config):
  from app.services.destination_policy import validate_origin
  validate_origin(config['origin'], config.get('allowed_hosts'))
  self.config=config
 def request(self,payload=None,body=None,query='',lease=None):
  endpoint='/api/worker-upload'+query if body is not None else '/api/worker'
  data=body if body is not None else json.dumps(payload).encode()
  cfg=self.config;headers={'Authorization':'Basic '+base64.b64encode((cfg['username']+':'+cfg['password']).encode()).decode(),'X-Worker-Token':cfg['worker_token'],'Content-Type':'application/octet-stream' if body is not None else 'application/json'}
  if lease:headers['X-Job-Lease']=lease
  req=urllib.request.Request(cfg['origin']+endpoint,data=data,headers=headers)
  try:
   with urllib.request.urlopen(req,timeout=90) as r:return json.load(r)
  except urllib.error.HTTPError as e:
   try:message=json.loads(e.read()).get('error','Erreur HTTP')
   except Exception:message='Erreur HTTP '+str(e.code)
   raise ValueError(message) from None

def copy_file(src,dst):
 if Path(src).suffix.lower() in {'.wav','.m4a','.mp4','.png','.jpg','.jpeg','.webp','.zip'}:os.link(src,dst)
 else:shutil.copy2(src,dst)
 return dst

def process(job,config):
 repo=Path(config['repository']);original=repo/'output/example/editorial-workflow'
 ident=job['id']
 if len(ident)!=32 or not ident.isalnum():raise ValueError('Identifiant de tâche invalide.')
 root=repo/'output/remote-worker'/ident
 root.mkdir(parents=True,exist_ok=True)
 cached=root/'result.json'
 if cached.exists():return json.loads(cached.read_text()),root
 isolated=root/'workflow'
 if not isolated.exists():shutil.copytree(original,isolated,copy_function=copy_file,ignore=shutil.ignore_patterns('__pycache__','numba-cache','*.part','*.lock'))
 workflow=EditorialWorkflow(isolated);story=copy.deepcopy(job['payload']['story'])
 packet=place_packet(workflow,story['place_id'])
 if story['source_fingerprints']!={s['id']:s['sha256'] for s in packet['evidence']} or story['scope_fingerprint']!=fingerprint({'scope':packet['place']['scope'],'points':packet['place']['points']}) or any(e['status']!='unchanged' for e in packet['evidence']):raise ValueError('Les sources du Mac et du site diffèrent : synchronisation du dossier nécessaire.')
 (root/'editorial-memory.json').write_text(json.dumps(job['payload'].get('editorial_memory',{}),ensure_ascii=False,indent=2)+'\n')
 audit=review_story(packet,story['draft'])
 if audit['reference_errors']:raise ValueError('Les références du récit sont à revoir.')
 pipeline=ReelPipeline(workflow);pipeline.config=copy.deepcopy(pipeline.config);pipeline.config['narration']['tempo']=job['tempo']
 pipeline.store.write('story-proposals/'+story['id']+'.json',story,immutable=True)
 if job['kind']=='voice':
  a=audition(pipeline,story['place_id'],story=story)
  if a['status']!='ready_to_listen':raise ValueError('Essai conservé sur le Mac : contrôle de voix à reprendre avant livraison.')
  shutil.copyfile(pipeline.store.path(a['audio_path']),root/'voice.wav')
  result={'audition':a,'files':{'voice.wav':hashlib.sha256((root/'voice.wav').read_bytes()).hexdigest()}}
 elif job['kind']=='render':
  p=copy.deepcopy(job['payload']['project']);p['draft']=story['draft'];p['runs']=[];p['approval']=None;p['visual_lot_approval']=None;p['exports']=[]
  proof=job['payload'].get('approved_narration')
  if proof:
   if proof['story_id']!=story['id']:raise ValueError('Voix validée liée à un autre récit.')
   target=pipeline.store.path('approved-audio/'+proof['sha256']+'.wav');target.parent.mkdir(parents=True,exist_ok=True)
   if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=proof['sha256']:
    headers={'Authorization':'Basic '+base64.b64encode((config['username']+':'+config['password']).encode()).decode()}
    req=urllib.request.Request(config['origin']+'/files?path='+urllib.parse.quote(proof['audio_path']),headers=headers)
    with urllib.request.urlopen(req,timeout=90) as response:body=response.read()
    if hashlib.sha256(body).hexdigest()!=proof['sha256']:raise ValueError('Voix validée modifiée pendant le transfert.')
    target.write_bytes(body)
   p['approved_narration']=dict(proof,audio_path=str(target.relative_to(pipeline.store.root)))
   if proof.get('music'):
    music=copy.deepcopy(proof['music']);mix=pipeline.store.path('approved-music/'+music['sha256']+'.mp3');mix.parent.mkdir(parents=True,exist_ok=True)
    if not mix.exists() or hashlib.sha256(mix.read_bytes()).hexdigest()!=music['sha256']:
     headers={'Authorization':'Basic '+base64.b64encode((config['username']+':'+config['password']).encode()).decode()}
     req=urllib.request.Request(config['origin']+'/files?path='+urllib.parse.quote(music['audio_path']),headers=headers)
     with urllib.request.urlopen(req,timeout=90) as response:body=response.read()
     if hashlib.sha256(body).hexdigest()!=music['sha256']:raise ValueError('Mix musical modifié pendant le transfert.')
     mix.write_bytes(body)
    music['audio_path']=str(mix.relative_to(pipeline.store.root));p['approved_narration']['music']=music
  pipeline.validate_copy(p['draft'],packet['place'])
  pipeline.store.write(pipeline.path(p['id']),p)
  run=pipeline.start_preview(p['id'],job['actor'],'Demande de maquette depuis le studio privé, tâche '+ident+'. Aucun accord factuel ou de publication.')
  pipeline.render(p['id'],run['id'])
  run=next(r for r in pipeline.read(p['id'])['runs'] if r['id']==run['id'])
  if run['status']!='rendered' or not run['technical']['passed']:raise ValueError(run.get('error') or 'Rendu non conforme aux contrôles techniques.')
  for name in run['files']:shutil.copyfile(pipeline.store.path(run['video']).parent/name,root/name)
  technical=copy.deepcopy(run['technical']);technical.pop('probe',None)
  result={'run':{'technical':technical,'error':None,'timeline':run.get('timeline',[]),'music':run.get('music'),'approved_narration':job['payload'].get('approved_narration')},'files':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('reel.mp4','caption.txt','subtitles.srt')}}
 else:raise ValueError('Type de tâche non prévu.')
 cached.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return result,root

def sync_feedback(client,config):
 data=client.request({'action':'sync'})
 from app.services.acquisition_store import AcquisitionStore
 workflow=EditorialWorkflow(Path(config['repository'])/'output/example/editorial-workflow')
 store=AcquisitionStore(workflow.store.root/'reels')
 for record in data['reviews']:
  ident=record['id']
  if len(ident)!=32 or not ident.isalnum():raise ValueError('Retour distant invalide.')
  store.write('inbox/'+ident+'.json',record,immutable=True)
 for story in data['stories']:
  story={k:v for k,v in story.items() if k not in {'stale','audition'}}
  packet=place_packet(workflow,story['place_id'])
  if story['source_fingerprints']!={s['id']:s['sha256'] for s in packet['evidence']} or review_story(packet,story['draft'])['reference_errors']:continue
  expected=fingerprint({k:v for k,v in story.items() if k not in {'id','created_at'}})
  if story['id']!=expected:raise ValueError('Empreinte du récit distant différente.')
  store.write('story-proposals/'+story['id']+'.json',story,immutable=True)
 workflow.store.write('editorial-memory/remote-policy.json',data['policy'])
 return [r['id'] for r in data['reviews']]

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true');parser.add_argument('--approved-once',action='store_true');parser.add_argument('--approved-listen',action='store_true');args=parser.parse_args()
 config=json.loads(Path(args.config).read_text());client=Client(config);last_sync=0;cursor=None
 # One local listener only; the old general voice queue remains paused.
 import fcntl
 lock_path=Path(config['repository'])/'output/immediate-listener.lock';lock_path.parent.mkdir(parents=True,exist_ok=True)
 listener_lock=lock_path.open('a')
 if args.approved_listen:fcntl.flock(listener_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 while True:
  job=None;failed=False
  try:
   if args.approved_listen or time.monotonic()-last_sync>60:
    received=sync_feedback(client,config);last_sync=time.monotonic()
    if args.approved_listen:client.request({'action':'ack_feedback','reviews':received})
   job=client.request({'action':'claim_approved' if args.approved_once or args.approved_listen else 'claim'}).get('job')
   if job:
    print('Tâche '+job['id']+' '+job['kind']+' démarrée.',flush=True)
    stop=threading.Event()
    def heartbeat(active_job=job,done=stop):
     while not done.wait(20):
      try:client.request({'action':'heartbeat','job':active_job['id'],'lease':active_job['lease_token']})
      except Exception:pass
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
     result,root=process(job,config)
     for name,sha in result['files'].items():
      query='?job='+job['id']+'&name='+urllib.parse.quote(name)
      for attempt in range(3):
       try:
        response=client.request(body=(root/name).read_bytes(),query=query,lease=job['lease_token'])
        if response['sha256']!=sha:raise ValueError('Empreinte de transfert différente.')
        break
       except Exception:
        if attempt==2:raise
        time.sleep(3)
     client.request({'action':'finish','job':job['id'],'lease':job['lease_token'],'result':result})
     print('Tâche '+job['id']+' livrée.',flush=True)
    finally:stop.set();thread.join(timeout=1)
  except Exception as error:
   failed=True
   # Never log headers, credential configuration or signed URLs.
   message=str(error)[:500]
   print('Essai interrompu : '+message,flush=True)
   if job:
    try:client.request({'action':'finish','job':job['id'],'lease':job['lease_token'],'result':{'error':message}})
    except Exception:pass
  if args.once or args.approved_once:
   if failed:raise SystemExit(1)
   break
  if args.approved_listen and not failed:
   client.request({'action':'heartbeat'})
   if not job:
    try:cursor=client.request({'action':'wait_feedback','cursor':cursor}).get('cursor')
    except Exception:time.sleep(3)
  else:time.sleep(10)
if __name__=='__main__':main()
