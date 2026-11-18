"""Durable, version-bound local studio work. No scheduled Codex heartbeat required."""
import copy, fcntl, hashlib, json
from contextlib import contextmanager
from datetime import datetime, UTC, timedelta
from pathlib import Path
from app.services.acquisition_store import AcquisitionStore

def now(): return datetime.now(UTC).isoformat()
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class StudioTasks:
    def __init__(self, workflow):
        self.workflow=workflow
        self.store=AcquisitionStore(workflow.store.root/'immediate-tasks')
    @contextmanager
    def lock(self):
        self.store.root.mkdir(parents=True,exist_ok=True)
        with self.store.path('tasks.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX);yield
    def list(self):
        return sorted((json.loads(p.read_text()) for p in self.store.root.glob('*.json') if p.name!='engine.json'),key=lambda x:x['created_at'])
    def enqueue(self, review):
        if review['decision']=='keep' and review['kind']=='run':return None
        ident=review['id']
        if len(ident)!=32 or not ident.isalnum():raise ValueError('Identifiant de retour invalide.')
        with self.lock():
            old=self.store.read(ident+'.json')
            if old:return old
            from app.services.production_chain import VERSION
            task={'id':ident,'place_id':review['place_id'],'review_ids':[ident], 'review':copy.deepcopy(review),'status':'queued','created_at':now(),'received_at':now(),'message':'Demande enregistrée. La version corrigée sera annoncée après sa livraison.','attempts':0,'origin':review.get('origin','studio'),'chain_version':VERSION}
            context=review.get('correction') or {}
            if review['decision']=='changes' and context.get('category') not in {None,'other'} and not context.get('diagnosis','').strip():
                from app.services.editorial_inbox import EditorialInbox
                from app.services.reel_pipeline import ReelPipeline
                history=[r for r in EditorialInbox(self.workflow,ReelPipeline(self.workflow)).list() if r['place_id']==review['place_id'] and r['id']!=ident]
                versions=set()
                for previous in reversed(history):
                    previous_context=previous.get('correction') or {}
                    if previous['decision']!='changes' or previous_context.get('category')!=context['category'] or previous_context.get('diagnosis'):break
                    versions.add(previous.get('audition_sha256') or previous['target_id'])
                if len(versions)>=2:
                    task.update(status='diagnosis_required',message='Deux versions ont été refusées sur ce point. Précisez le passage, la cause observée et le résultat attendu avant un nouvel essai.')
            self.store.write(ident+'.json',task,immutable=True)
            return task

    def request_production(self, place, instruction, *, origin, request_key):
        """One queue for chat and site. A production request is NOT an owner approval."""
        from app.services.production_chain import VERSION
        from app.services.generation_quality import current_voice_approved
        from app.services.reel_pipeline import ReelPipeline
        if origin not in {'chat','studio'} or not isinstance(request_key,str) or not 8 <= len(request_key) <= 120:
            raise ValueError('Origine et clé stable de demande requises.')
        if not isinstance(instruction,str) or not instruction.strip() or len(instruction)>5000:
            raise ValueError('Consigne de production requise (5 000 caractères maximum).')
        state=self.workflow.read()
        if place not in state['places'] or state['places'][place]['owner_interest']=='rejected':
            raise ValueError('Lieu inconnu ou rejeté.')
        ident=digest({'origin':origin,'request_key':request_key})[:32]
        with self.lock():
            existing=self.store.read(ident+'.json')
            if existing:
                if existing['place_id']!=place or existing['review']['note']!=instruction.strip():
                    raise ValueError('Clé déjà utilisée pour une autre demande.')
                return existing
            before=self.snapshot(place)
            if before['story'] and current_voice_approved(self.workflow,ReelPipeline(self.workflow),before['story']):
                raise ValueError('Cette voix est validée : conserver son accord et utiliser sa demande de montage ou un retour ciblé.')
            active=[t for t in self.list() if t['place_id']==place and t['status'] in {'queued','interpreting','producing','verifying','waiting_connection'}]
            if active:
                raise ValueError('Une demande est déjà en cours pour ce lieu.')
            review={'id':ident,'place_id':place,'kind':'story','decision':'produce',
                    'target_id':(before['story'] or {}).get('id'),'note':instruction.strip(),
                    'audition_sha256':(before['story'] or {}).get('audition',{}).get('sha256'),
                    'created_at':now(),'origin':origin,'publication_approval':False}
            row={'id':ident,'place_id':place,'review_ids':[],'review':review,'origin':origin,
                 'request_type':'production','input_version':digest({k:v for k,v in before.items() if k!='memory_version'}),
                 'chain_version':VERSION,'status':'queued','attempts':0,'created_at':now(),'received_at':now(),
                 'message':'Production enregistrée ; vérification des photos avant la voix.'}
            self.store.write(ident+'.json',row,immutable=True)
            return row
    def update(self, ident, **values):
        with self.lock():
            row=self.store.read(ident+'.json')
            if all(row.get(k)==v for k,v in values.items()):return row
            row.update(values,updated_at=now());self.store.write(ident+'.json',row);return row
    def claim(self):
        with self.lock():
            rows=self.list()
            if any(r['status'] in {'interpreting','producing','verifying'} for r in rows):return None
            pending=[r for r in rows if r['status']=='queued' or (r['status']=='waiting_connection' and r.get('retry_at','')<=now())]
            if not pending:return None
            priorities=self.workflow.store.read('studio-operating.json',{}).get('priority_places',[])
            pending.sort(key=lambda r:(0 if r['place_id'] in priorities else 1,r['created_at']))
            first=pending[0]
            # Combine successive unprocessed comments on the same version, retaining every request.
            group=[r for r in pending if r['place_id']==first['place_id'] and r['review']['target_id']==first['review']['target_id'] and r['review']['decision']=='changes' and first['review']['decision']=='changes'] or [first]
            row=group[-1];row['review_ids']=list(dict.fromkeys(i for r in group for i in r['review_ids']))
            requests={q['review_id']:q for r in group for q in r.get('correction_requests',[
                {'review_id':r['review']['id'],'note':r['review'].get('note',''),
                 'category':(r['review'].get('correction') or {}).get('category','other')}])}
            row['correction_requests']=list(requests.values())
            for previous in group[:-1]:
                previous.update(status='combined',combined_into=row['id'],message='Traitée avec votre commentaire suivant.');self.store.write(previous['id']+'.json',previous)
            row.update(status='interpreting',started_at=now(),attempts=row['attempts']+1,message='Interprétation de votre demande en cours sur le Mac.')
            self.store.write(row['id']+'.json',row);return row

    def retry(self, ident):
        if not isinstance(ident,str) or len(ident)!=32 or not ident.isalnum():raise ValueError('Identifiant invalide.')
        with self.lock():
            row=self.store.read(ident+'.json')
            if not row or row['status'] not in {'blocked','interrupted','waiting_connection','diagnosis_required'}:
                raise ValueError('Cette demande ne peut pas être reprise dans son état actuel.')
            if row['status']=='diagnosis_required':raise ValueError('Ajoutez un diagnostic ciblé dans un nouveau retour avant un autre essai.')
            row.setdefault('attempt_history',[]).append({k:row.get(k) for k in ('attempts','started_at','updated_at','status','message')})
            row.update(status='queued',retry_at=None,updated_at=now(),retry_token=now(),message='Reprise ciblée demandée : dernier refus conservé, nouvelle revue seulement après correction vérifiée.')
            self.store.write(ident+'.json',row)
            return row

    def fail(self, ident, error, *, transient=False):
        row=self.store.read(ident+'.json')
        attempts=row.get('attempts',1)
        retry=transient and attempts<3
        # The unpublished batch can have unrelated prior visual/audio failures.
        # Bound network retries by consecutive transient failures, not total attempts.
        if transient and '[CONTENT-UNPUBLISHED-BATCH]' in row.get('review',{}).get('note',''):
            consecutive=0
            for previous in reversed(row.get('attempt_history',[])):
                if 'transient' not in previous:continue
                if not previous['transient']:break
                consecutive+=1
            retry=consecutive<2
        history=row.get('attempt_history',[])+[{'attempt':attempts,'ended_at':now(),'error':str(error)[:700],'transient':transient}]
        return self.update(ident,status='waiting_connection' if retry else 'blocked',
            retry_at=(datetime.now(UTC)+timedelta(seconds=min(900,60*2**max(0,attempts-1)))).isoformat() if retry else None,
            attempt_history=history,message=('Connexion interrompue. Reprise contrôlée prévue. ' if retry else '')+str(error)[:600])

    def restart_current_context(self, ident):
        """Fresh context for the same real request; preserve the obsolete attempt."""
        from uuid import uuid4
        from app.services.studio_task_worker import valid_current
        with self.lock():
            old = self.store.read(ident+'.json')
            if old.get('restarted_as'):
                return self.store.read(old['restarted_as']+'.json')
            if not valid_current(old, self.snapshot(old['place_id'])):
                raise ValueError('Demande remplacée ; ne pas relancer un ancien retour.')
            row = {k:copy.deepcopy(old[k]) for k in ('place_id','review','review_ids','origin','chain_version','request_type','input_version','retry_token','correction_requests') if k in old}
            row.update(id=uuid4().hex, status='queued', created_at=now(), received_at=old['received_at'],
                       attempts=0, continued_from=ident,
                       message='Reprise avec le contexte actuel ; essais précédents conservés.')
            self.store.write(row['id']+'.json',row,immutable=True)
            old.update(status='superseded',restarted_as=row['id'],updated_at=now(),
                       message='Essai conservé ; reprise dans une nouvelle copie avec vos consignes actuelles.')
            self.store.write(ident+'.json',old)
            return row

    def snapshot(self,place):
        from app.services.reel_story import StoryPreparation
        from app.services.editorial_inbox import EditorialInbox
        from app.services.reel_pipeline import ReelPipeline
        pipeline=ReelPipeline(self.workflow)
        reviews=[r for r in EditorialInbox(self.workflow,pipeline).list() if r['place_id']==place]
        story=StoryPreparation(self.workflow).latest(place)
        if story and story.get('stale'):
            from app.services.reel_story import place_packet, fingerprint
            packet=place_packet(self.workflow,place)
            current={e['id']:e['sha256'] for e in packet['evidence']}
            previous=story['source_fingerprints']
            scope=fingerprint({'scope':packet['place']['scope'],'points':packet['place']['points']})
            story['additive_sources_only']=(len(current)>len(previous) and
                all(current.get(k)==v for k,v in previous.items()) and
                all(e['status']=='unchanged' for e in packet['evidence']) and
                story['scope_fingerprint']==scope)
        projects=[pipeline.read(p.parent.name) for p in pipeline.store.root.glob('projects/*/project.json')]
        from app.services.editorial_memory import memory_view,production_memory_version
        return {'place_id':place,'production_memory_version':production_memory_version(self.workflow,place),'memory_version':memory_view(self.workflow,place)['fingerprint'],'reviews':reviews,'story':story,'projects':sorted([p for p in projects if p['place_id']==place],key=lambda p:p['id']), 'music':self.workflow.store.read('music-plans.json',{}).get(place),'visuals':self.workflow.store.read('visual-audits.json',{}).get(place)}
