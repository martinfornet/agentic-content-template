"""Version-bound editorial checklist, evidence ledger, storyboard and owner observations."""
import copy,fcntl,json,math,re
from datetime import datetime,UTC
from pathlib import Path
from urllib.parse import urlparse
from app.services.studio_tasks import digest,now
from app.services.editorial_inbox import EditorialInbox

GRID=[('distinctiveness','Pourquoi raconter ce lieu ?'),('hook','L’accroche contient-elle un détail précis ?'),('progression','Chaque passage apporte-t-il une preuve nouvelle ?'),('orality','Le texte est-il fluide à voix haute ?'),('payoff','La conclusion répond-elle à la promesse initiale ?')]

VISUAL_GRID=[('visual_selection','Chaque image apporte-t-elle un détail fort et pertinent sur ce lieu ?'),('visual_progression','Les sujets et les cadrages font-ils progresser le regard sans répétition ni remplissage ?'),('editing_rhythm','Les durées et les transitions gardent-elles le rythme sans précipiter le récit ?'),('image_sound_coherence','Les images, les mots, la musique et les sous-titres fonctionnent-ils ensemble ?'),('final_montage','Le montage complet, de l’accroche à la fin, est-il convaincant et sans faiblesse identifiée ?')]

def target_version(target,kind):
    return digest({'kind':kind,'id':target['id'],'draft':target.get('draft') or target.get('inputs',{}).get('draft'),
                   'media':target.get('sha256') if kind=='run' else target.get('audition',{}).get('sha256')})

class QualityDossier:
    def __init__(self,workflow,pipeline):self.workflow,self.pipeline=workflow,pipeline
    def target(self,kind,ident):return EditorialInbox(self.workflow,self.pipeline).target(kind,ident)
    def build(self,kind,ident):
        target=self.target(kind,ident);place=target['place_id'];version=target_version(target,kind)
        draft=target.get('draft') or target.get('inputs',{}).get('draft',{})
        saved=self.workflow.store.read('quality/'+version+'.json',{'sections':{}})
        facts=[]
        for index,beat in enumerate(draft.get('beats',[])):
            for evidence in beat.get('evidence',[]):
                facts.append({'id':digest([index,evidence])[:20],'beat':index+1,'claim':beat['text'],'evidence':evidence,'status':'unverified'})
        project=self.pipeline.read(target['project_id']) if kind=='run' else None
        assets=(target.get('inputs',{}).get('assets') or (project or {}).get('assets') or {})
        recipe=target.get('production_recipe') or (project or {}).get('production_recipes',{}).get(ident)
        shots=(recipe or {}).get('shots') or [{'beat':v.get('beat_index',0)+1,'asset':v.get('asset'),'source':v.get('clip',{}).get('page_url'),'start':v.get('start'),'end':v.get('end')} for v in target.get('timeline',[])] or [{'beat':int(k)+1,'asset':v} for k,v in assets.items()]
        sources=self.workflow.read()['places'][place].get('sources',[])
        glossary=Path(self.workflow.read()['repository'])/'brands/example/pronunciation.json'
        pronunciations=json.loads(glossary.read_text()).get('entries',[]) if glossary.exists() else []
        joined=' '.join(b['text'] for b in draft.get('beats',[]))
        checks=[{'id':k,'question':q,'status':'to_review'} for k,q in GRID+(VISUAL_GRID if kind=='run' else [])]
        from app.services.editorial_memory import memory_view
        return {'kind':kind,'target_id':ident,'version':version,'place_id':place,'editorial_grid':checks,
                'facts':facts,'sources':sources,'storyboard':shots,'recipe':recipe,'annotations':saved,
                'pronunciations':[r for r in pronunciations if re.search(re.escape(r['written']),joined,re.I)],
                'generation':draft.get('generation'),
                'voice_selection':self.pipeline.store.read(target.get('audition',{}).get('selection_path','missing-selection.json')),
                'montage_review':self.pipeline.store.read(target.get('artistic_review_path','missing-artistic-review.json')),
                'charter_version':memory_view(self.workflow,place)['charter_version'],
                'technical_status':target.get('technical') if kind=='run' else target.get('audition',{}).get('technical_audio'),
                'semantic_status':'owner_record_required','perceptual_status':'owner_listening_required',
                'music':target.get('music') or (target.get('approved_narration') or {}).get('music') or self.workflow.store.read('music-plans.json',{}).get(place),
                'storyboard_status':'documented' if shots else 'to_prepare','publication_authorized':False}
    def record(self,kind,ident,version,section,entries):
        if section not in {'editorial','facts','rights','storyboard'}:raise ValueError('Section inconnue.')
        if not isinstance(entries,list) or len(entries)>100:raise ValueError('Liste de contrôles requise (100 maximum).')
        for row in entries:
            if not isinstance(row,dict) or set(row)-{'id','status','note','source','checked_at'}:raise ValueError('Contrôle invalide.')
            if row.get('status') not in {'pending','verified','rejected','not_applicable'}:raise ValueError('État de contrôle invalide.')
            if not isinstance(row.get('id'),str) or not 1<=len(row['id'])<=100:raise ValueError('Identifiant de contrôle requis.')
            if any(not isinstance(row.get(k,''),str) or len(row.get(k,''))>5000 for k in ('note','source','checked_at')):raise ValueError('Texte de contrôle invalide.')
            if row['status'] in {'verified','not_applicable'} and not row.get('note','').strip():raise ValueError('Précisez le résultat du contrôle.')
            if section in {'facts','rights'} and row['status']=='verified' and (not row.get('source') or not row.get('checked_at')):raise ValueError('Source et date du contrôle requises.')
            if row.get('checked_at'):
                try:datetime.fromisoformat(row['checked_at'])
                except ValueError:raise ValueError('Date de contrôle invalide.')
        current=self.build(kind,ident)
        allowed={'editorial':{r['id'] for r in current['editorial_grid']},'facts':{r['id'] for r in current['facts']},'rights':{'music','images'},'storyboard':{'images'}}[section]
        if any(row['id'] not in allowed for row in entries):raise ValueError('Élément incompatible avec cette rubrique.')
        root=self.workflow.store.root/'reels/inbox';root.mkdir(parents=True,exist_ok=True)
        with (root/'review.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if target_version(self.target(kind,ident),kind)!=version:raise ValueError('Version modifiée. Rechargez avant de confirmer ce contrôle.')
            key='quality/'+version+'.json';old=self.workflow.store.read(key,{'sections':{},'history':[]})
            old['history'].append({'section':section,'previous':old['sections'].get(section),'at':now(),'actor':self.workflow.read()['owner_id']})
            merged={row['id']:row for row in old['sections'].get(section,[])}
            merged.update({row['id']:copy.deepcopy(row) for row in entries})
            old['sections'][section]=list(merged.values());old.update(version=version,target_id=ident,kind=kind,updated_at=now())
            self.workflow.store.write(key,old)
            return old


def correction_context(value,target):
    if value is None:return None
    if not isinstance(value,dict) or set(value)-{'category','beat','seconds','diagnosis'}:raise ValueError('Contexte de correction invalide.')
    if value.get('category') not in {'text','pronunciation','rhythm','music','image','other'}:raise ValueError('Catégorie invalide.')
    result=copy.deepcopy(value)
    beat=value.get('beat');seconds=value.get('seconds')
    beats=(target.get('draft') or target.get('inputs',{}).get('draft',{})).get('beats',[])
    if beat is not None and (type(beat) is not int or not 1<=beat<=len(beats)):raise ValueError('Passage invalide.')
    duration=(target.get('audition') or target.get('technical') or {}).get('duration')
    if seconds is not None and (type(seconds) not in (float,int) or not math.isfinite(seconds) or seconds<0 or (duration and seconds>duration)):raise ValueError('Instant invalide.')
    if not isinstance(value.get('diagnosis',''),str) or len(value.get('diagnosis',''))>3000:raise ValueError('Diagnostic trop long.')
    return result
