"""Durable, leased preview jobs. Publication and factual approvals are excluded."""
from datetime import UTC,datetime,timedelta
import copy,fcntl,hashlib,json,secrets
from uuid import uuid4
from app.services.acquisition_store import AcquisitionStore
from app.services.editorial_inbox import EditorialInbox
from app.services.reel_story import StoryPreparation
from app.services.editorial_memory import memory_view


def now():return datetime.now(UTC)
class ReelJobs:
 def __init__(self,workflow,pipeline):
  self.workflow,self.pipeline=workflow,pipeline
  self.store=AcquisitionStore(workflow.store.root/'worker-jobs')
 def lock(self):
  self.store.root.mkdir(parents=True,exist_ok=True)
  f=self.store.path('queue.lock').open('a');fcntl.flock(f,fcntl.LOCK_EX);return f
 def read(self,ident):
  if not isinstance(ident,str) or len(ident)!=32 or not ident.isalnum():raise ValueError('Tâche inconnue.')
  job=self.store.read(ident+'.json')
  if not job:raise ValueError('Tâche inconnue.')
  return job
 def list(self):
  with self.lock():
   rows=[]
   for p in self.store.root.glob('*.json'):
    j=json.loads(p.read_text())
    if 'kind' not in j:continue
    if j['status']=='running' and datetime.fromisoformat(j['lease_until'])<now():
     j.update(status='failed',error='Connexion du Mac interrompue. Vous pouvez relancer cet essai.');self.store.write(p.name,j)
    rows.append({k:v for k,v in j.items() if k not in {'payload','lease_token'}})
   return sorted(rows,key=lambda j:j['created_at'])
 def enqueue(self,kind,story_id,tempo=1.03):
  if kind not in {'voice','render'}:raise ValueError('Type d’essai inconnu.')
  if isinstance(tempo,bool) or not isinstance(tempo,(float,int)) or tempo not in {0.95,1.0,1.03,1.08}:raise ValueError('Rythme non proposé.')
  story=EditorialInbox(self.workflow,self.pipeline).target('story',story_id)
  project=None
  if kind=='render':
   candidates=[self.pipeline.read(p.parent.name) for p in self.pipeline.store.root.glob('projects/*/project.json')]
   project=next((p for p in candidates if p['place_id']==story['place_id'] and p.get('assets')),None)
   if not project or len(project['assets'])!=len(story['draft']['beats']):raise ValueError('Préparer un lot visuel complet pour ce récit avant le montage.')
  with self.lock():
   if self.store.read('production-control.json',{}).get('paused'):raise ValueError('Production suspendue à votre demande : analyse des références en cours.')
   for p in self.store.root.glob('*.json'):
    j=json.loads(p.read_text())
    if j.get('kind')==kind and j.get('story_id')==story_id and j.get('tempo')==tempo and j['status'] in {'queued','running'}:return {k:v for k,v in j.items() if k not in {'payload','lease_token'}}
   job={'id':uuid4().hex,'kind':kind,'story_id':story_id,'place_id':story['place_id'],'tempo':tempo,'status':'queued','created_at':now().isoformat(),'actor':self.workflow.read()['owner_id'],'error':None,'payload':{'story':{k:v for k,v in story.items() if k not in {'audition','stale'}},'project':project,'editorial_memory':memory_view(self.workflow,story['place_id'])},'publication_approval':False,'factual_approval':False}
   self.store.write(job['id']+'.json',job,immutable=True)
   return {k:v for k,v in job.items() if k!='payload'}
 def claim(self):
  self.list()
  with self.lock():
   if self.store.read('production-control.json',{}).get('paused'):return None
   self.store.write('heartbeat.json',{'last_seen':now().isoformat()})
   rows=[json.loads(p.read_text()) for p in self.store.root.glob('*.json')]
   jobs=sorted([j for j in rows if j.get('status')=='queued'],key=lambda j:j['created_at'])
   if not jobs:return None
   j=jobs[0];j.update(status='running',lease_token=secrets.token_urlsafe(32),lease_until=(now()+timedelta(minutes=30)).isoformat())
   self.store.write(j['id']+'.json',j);return j
 def heartbeat(self,ident=None,lease=None):
  with self.lock():
   self.store.write('heartbeat.json',{'last_seen':now().isoformat()})
   if ident:
    j=self.leased(ident,lease);j['lease_until']=(now()+timedelta(minutes=30)).isoformat();self.store.write(ident+'.json',j)
  return {'ok':True}
 def leased(self,ident,lease):
  j=self.read(ident)
  if j['status']!='running' or datetime.fromisoformat(j['lease_until'])<now() or not isinstance(lease,str) or not secrets.compare_digest(j.get('lease_token',''),lease):raise ValueError('Tâche non attribuée à ce moteur.')
  return j
 def status(self):
  v=self.store.read('heartbeat.json',{})
  return dict(v,feedback_automation=bool(self.store.read('production-control.json',{}).get('feedback_automation')),paused=bool(self.store.read('production-control.json',{}).get('paused')),online=bool(v and now()-datetime.fromisoformat(v['last_seen'])<timedelta(seconds=90)))
 def upload(self,ident,lease,name,body):
  if name not in {'voice.wav','reel.mp4','caption.txt','subtitles.srt'}:raise ValueError('Fichier de résultat non prévu.')
  with self.lock():
   j=self.leased(ident,lease)
   if (j['kind']=='voice' and name!='voice.wav') or (j['kind']=='render' and name=='voice.wav'):raise ValueError('Fichier incompatible avec cet essai.')
   p=self.pipeline.store.path('worker-results/'+ident+'/'+name);p.parent.mkdir(parents=True,exist_ok=True)
   tmp=p.with_suffix(p.suffix+'.part');tmp.write_bytes(body);tmp.replace(p)
  return {'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body)}
 def finish(self,ident,lease,result):
  with self.lock():
   j=self.read(ident)
   if j['status']=='completed' and isinstance(lease,str) and secrets.compare_digest(j.get('lease_token',''),lease):return {'ok':True}
   j=self.leased(ident,lease)
   if result.get('error'):
    j.update(status='failed',error=str(result['error'])[:1000],finished_at=now().isoformat());self.store.write(ident+'.json',j);return {'ok':True}
   if j.get('render_request_id'):
    story=StoryPreparation(self.workflow).latest(j['place_id'])
    reviews=[v for v in EditorialInbox(self.workflow,self.pipeline).list() if v['place_id']==j['place_id']]
    proof=j['payload']['approved_narration']
    if not story or story['stale'] or story['id']!=j['story_id'] or story.get('audition',{}).get('sha256')!=proof['sha256'] or not reviews or reviews[-1]['target_id']!=j['story_id'] or reviews[-1].get('audition_sha256')!=proof['sha256'] or reviews[-1]['decision']!='keep' or reviews[-1].get('music')!=proof.get('music'):
     raise ValueError('Validation modifiée pendant le montage : résultat conservé, non livré.')
    if result.get('run',{}).get('approved_narration')!=proof or result.get('run',{}).get('technical',{}).get('passed') is not True:
     raise ValueError('Le montage doit conserver la voix validée et réussir les contrôles.')
   required=['voice.wav'] if j['kind']=='voice' else ['reel.mp4','caption.txt','subtitles.srt']
   for name in required:
    p=self.pipeline.store.path('worker-results/'+ident+'/'+name)
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=result.get('files',{}).get(name):raise ValueError('Résultat incomplet ou empreinte incorrecte.')
   if j['kind']=='voice':
    a=result['audition'];a.update(story_id=j['story_id'],place_id=j['place_id'],audio_path='worker-results/'+ident+'/voice.wav',sha256=result['files']['voice.wav'],created_at=now().isoformat(),listening_approved=False,approval_created=False,publication_choice_created=False,worker_job_id=ident)
    self.pipeline.store.write('story-proposals/'+j['story_id']+'/auditions/'+ident+'/audition.json',a)
   else:
    r=result['run'];r.update(id=ident,status='rendered',stage='completed',mode='preview',video='worker-results/'+ident+'/reel.mp4',sha256=result['files']['reel.mp4'],files={n:'worker-results/'+ident+'/'+n for n in required},qa=None,worker_job_id=ident)
    r['inputs']={'draft':j['payload']['story']['draft']};r['created_at']=now().isoformat()
    with self.pipeline.transaction(j['payload']['project']['id']) as project:
     if not any(old['id']==ident for old in project['runs']):project['runs'].append(r)
   j.update(status='completed',finished_at=now().isoformat(),result_file='worker-results/'+ident+'/'+required[0]);self.store.write(ident+'.json',j)
   return {'ok':True}
