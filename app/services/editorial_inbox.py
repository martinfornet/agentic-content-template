"""Version-bound editorial feedback. It never grants publication or factual approval."""
import copy
from datetime import UTC, datetime
import fcntl
import json
from uuid import uuid4
from app.services.acquisition_store import AcquisitionStore
from app.services.reel_story import StoryPreparation


class EditorialInbox:
    def __init__(self, workflow, pipeline):
        self.workflow, self.pipeline = workflow, pipeline
        self.store = AcquisitionStore(workflow.store.root / 'reels/inbox')

    def list(self):
        return sorted((json.loads(p.read_text()) for p in self.store.root.glob('*.json')), key=lambda r:r['created_at'])

    def target(self, kind, target_id):
        if kind == 'story':
            stories = StoryPreparation(self.workflow)
            # The immutable proposal identifies its place. Do not rebuild every
            # other place's learning packet just to locate this one story.
            saved = stories.store.read(target_id + '.json')
            if saved and saved.get('place_id') in self.workflow.read()['places']:
                value = stories.latest(saved['place_id'])
                if value and value['id'] == target_id:
                    if value['stale']:raise ValueError('Le dossier a changé : revoir les sources avant ce récit.')
                    return value
            raise ValueError('Ce récit a changé. Rechargez la version actuelle.')
        if kind == 'run':
            for path in self.pipeline.store.root.glob('projects/*/project.json'):
                project = self.pipeline.read(path.parent.name)
                run = next((r for r in project['runs'] if r['id'] == target_id), None)
                if run:
                    if run['status'] != 'rendered':raise ValueError('Le rendu doit être terminé avant revue.')
                    # Feedback can intentionally refer to an older rendered version.
                    return dict(run, place_id=project['place_id'], project_id=project['id'])
        raise ValueError('Élément de revue inconnu.')

    def submit(self, kind, target_id, decision, note, texts=None, caption=None, memory_scope="auto", audition_sha=None, music_choice=None, correction=None, origin='studio', photo_reuse=None):
        if origin not in {'studio','chat'}:raise ValueError('Origine du retour invalide.')
        if memory_scope not in {'auto','general','place'}:raise ValueError('Portée de mémoire invalide.')
        if decision not in {'keep','changes','edited'}:raise ValueError('Décision de revue invalide.')
        if not isinstance(note,str) or len(note.strip()) > 5000 or (decision != 'keep' and not note.strip()):raise ValueError('Précisez votre retour (5 000 caractères maximum).')
        if texts is not None and (kind != 'story' or decision != 'edited'):raise ValueError('Correction réservée au texte du récit.')
        if decision == 'edited' and texts is None:raise ValueError('Texte corrigé requis.')
        self.store.root.mkdir(parents=True,exist_ok=True)
        with self.store.path('review.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            target=self.target(kind,target_id)
            from app.services.quality_dossier import correction_context
            correction=correction_context(correction,target)
            reuse = None
            if photo_reuse is not None:
                if kind != 'run' or decision != 'changes' or (correction or {}).get('category') != 'image':
                    raise ValueError('Exception photo réservée à une correction d’image explicite sur un Reel.')
                from app.services.visual_reuse import prepare_authorization
                reuse = prepare_authorization(self.pipeline, target, photo_reuse)
            from app.services.editorial_memory import feedback_interpretation
            audio_sha=target.get('audition',{}).get('sha256') if kind=='story' else None
            if audition_sha is not None and audition_sha!=audio_sha:raise ValueError('La prise de voix a changé. Rechargez ce récit avant ce retour ; votre texte est conservé.')
            if kind=='story' and decision=='keep' and (not audio_sha or target.get('audition',{}).get('status')!='ready_to_listen' or audition_sha!=audio_sha):
                raise ValueError('Écoutez puis validez la prise disponible de ce récit.')
            from app.services.reel_music import choose_music
            music=choose_music(self.workflow,target,music_choice) if kind=='story' and decision=='keep' else None
            new_id=None
            if texts is not None:
                draft=copy.deepcopy(target['draft'])
                draft.pop('generation',None)  # A previous hook selection cannot describe owner-edited text.
                if not isinstance(texts,list) or len(texts)!=len(draft['beats']) or not all(isinstance(t,str) for t in texts):raise ValueError('Conservez les passages et leurs sources pour cette correction.')
                if not isinstance(caption,str) or not caption.strip():raise ValueError('Légende requise.')
                for beat,text in zip(draft['beats'],texts):beat['text']=text
                draft['caption']=caption
                new_id=StoryPreparation(self.workflow).prepare(target['place_id'],draft,author='owner')['id']
            value={'id':uuid4().hex,'origin':origin,'kind':kind,'target_id':target_id,'place_id':target['place_id'],
                   'correction':correction,'interpretation':feedback_interpretation(note.strip(),memory_scope),'decision':decision,'music':music,'audition_sha256':audio_sha,'memory_scope':memory_scope,'note':note.strip(),'new_story_id':new_id,
                   'actor':self.workflow.read()['owner_id'],'created_at':datetime.now(UTC).isoformat(),
                   'publication_approval':False,'factual_approval':False}
            if reuse is not None:value['photo_reuse']=reuse
            self.store.write(value['id']+'.json',value,immutable=True)
            from app.services.production_chain import ProductionChain
            ProductionChain(self.workflow).owner_feedback(value)
            if kind=='story' and decision=='keep' and audio_sha:
                from app.services.reel_render_requests import ReelRenderRequests
                ReelRenderRequests(self.workflow).enqueue(target,value)
            from app.services.studio_tasks import StudioTasks
            StudioTasks(self.workflow).enqueue(value)
            if kind=='run' and decision=='keep':
                from app.services.reel_covers import ReelCovers
                ReelCovers(self.workflow,self.pipeline).prepare(value)
            return value
