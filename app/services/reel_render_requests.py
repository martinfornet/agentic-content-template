"""Version-bound automatic montage requests, consumed by the local Agentic Content follow-up."""
from datetime import UTC, datetime
import hashlib,json,fcntl
from app.services.acquisition_store import AcquisitionStore

class ReelRenderRequests:
    def __init__(self, workflow):
        self.store=AcquisitionStore(workflow.store.root/'render-requests')
    def list(self):
        rows=[]
        for path in self.store.root.glob('*.json'):
            row=json.loads(path.read_text())
            if row.get('job_id') and row['status']!='cancelled':
                jobfile=self.store.root.parent/'worker-jobs'/(row['job_id']+'.json')
                if jobfile.exists():
                    job=json.loads(jobfile.read_text());row.update(status=job['status'],reason=job.get('error'))
            rows.append(row)
        return sorted(rows,key=lambda x:x['created_at'])
    def enqueue(self, story, review):
        audio=story.get('audition') or {}
        if review['decision']!='keep' or review['kind']!='story' or review['target_id']!=story['id']:
            return None
        if story.get('stale') or audio.get('status')!='ready_to_listen' or not audio.get('sha256') or review.get('audition_sha256')!=audio['sha256']:
            raise ValueError('Valider la voix disponible de ce récit avant le montage.')
        ident=hashlib.sha256((story['id']+':'+audio['sha256']+':'+json.dumps(review.get('music'),sort_keys=True)).encode()).hexdigest()[:32]
        self.store.root.mkdir(parents=True,exist_ok=True)
        with self.store.path('queue.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            old=self.store.read(ident+'.json')
            if old:
                if old['status'] in {'cancelled','failed'}:
                    old.update(status='queued',review_id=review['id'],reason=None)
                    old.pop('job_id',None);self.store.write(ident+'.json',old)
                return old
            value=dict(id=ident,place_id=story['place_id'],story_id=story['id'],audition_sha256=audio['sha256'],review_id=review['id'],music=review.get('music'),status='queued',created_at=datetime.now(UTC).isoformat(),publication_approval=False)
            self.store.write(ident+'.json',value,immutable=True)
            return value

    def claim_approved(self, workflow, pipeline):
        """Finite authorized follow-up only; never starts voice jobs or unpauses old worker."""
        from app.services.reel_story import StoryPreparation
        from app.services.editorial_inbox import EditorialInbox
        from app.services.reel_jobs import ReelJobs,now
        from app.services.reel_pipeline import digest
        from app.services.editorial_memory import memory_view
        from datetime import timedelta
        import secrets,copy
        from uuid import uuid4
        jobs=ReelJobs(workflow,pipeline)
        if not jobs.store.read('production-control.json',{}).get('feedback_automation'):return None
        self.store.root.mkdir(parents=True,exist_ok=True)
        with self.store.path('queue.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            reviews=EditorialInbox(workflow,pipeline).list()
            for request in self.list():
                if request['status'] in {'completed','cancelled','failed'}:continue
                story=StoryPreparation(workflow).latest(request['place_id'])
                last=[v for v in reviews if v['place_id']==request['place_id']]
                valid=story and not story['stale'] and story['id']==request['story_id'] and story.get('audition',{}).get('sha256')==request['audition_sha256'] and last and last[-1]['target_id']==story['id'] and last[-1].get('audition_sha256')==request['audition_sha256'] and last[-1]['decision']=='keep' and last[-1].get('music')==request.get('music')
                if not valid:
                    request.update(status='cancelled',reason='Le récit, la voix ou le retour a changé.');self.store.write(request['id']+'.json',request);continue
                if request.get('job_id'):
                    jobs.list();job=jobs.read(request['job_id'])
                    request.update(status=job['status'],reason=job.get('error'));self.store.write(request['id']+'.json',request)
                    if job['status']=='running':return None
                    if job['status'] in {'completed','failed'}:continue
                project=None
                for path in pipeline.store.root.glob('projects/*/project.json'):
                    candidate=pipeline.read(path.parent.name)
                    if candidate['place_id']!=request['place_id']:continue
                    candidate=copy.deepcopy(candidate);candidate['draft']=story['draft']
                    if len(candidate.get('assets',{}))!=len(story['draft']['beats']):continue
                    try:
                        if pipeline.visual_lot(candidate)['complete']:project=candidate;break
                    except (KeyError,ValueError):continue
                if project is None:
                    request.update(status='waiting_media',reason='Images du récit à préparer et contrôler.');self.store.write(request['id']+'.json',request);continue
                proof={'story_id':story['id'],'review_id':request['review_id'],'sha256':request['audition_sha256'],'audio_path':story['audition']['audio_path'],'draft_sha256':digest(story['draft'])}
                if request.get('music'):proof['music']=request['music']
                with jobs.lock():
                    ident=uuid4().hex;job={'id':ident,'kind':'render','story_id':story['id'],'place_id':story['place_id'],'tempo':1.0,'status':'running','created_at':now().isoformat(),'actor':workflow.read()['owner_id'],'error':None,'payload':{'story':{k:v for k,v in story.items() if k not in {'audition','stale'}},'project':project,'approved_narration':proof,'editorial_memory':memory_view(workflow,story['place_id'])},'publication_approval':False,'factual_approval':False,'render_request_id':request['id'],'lease_token':secrets.token_urlsafe(32),'lease_until':(now()+timedelta(minutes=30)).isoformat()}
                    jobs.store.write(ident+'.json',job)
                request.update(status='running',job_id=ident,reason=None);self.store.write(request['id']+'.json',request)
                return job
        return None
