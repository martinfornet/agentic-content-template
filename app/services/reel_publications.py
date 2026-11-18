"""Owner-declared publication archive. Never publishes to an external platform."""
import fcntl
import hashlib
import json
import re
import zipfile
from datetime import UTC, datetime
from app.services.acquisition_store import AcquisitionStore
from app.services.editorial_inbox import EditorialInbox

class ReelPublications:
    def __init__(self, workflow, pipeline):
        self.workflow, self.pipeline = workflow, pipeline
        self.store = AcquisitionStore(pipeline.store.root / 'published')

    def list(self):
        return {p.stem: json.loads(p.read_text()) for p in self.store.root.glob('*.json')}

    def mark(self, run_id, published=True, metadata=None):
        if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',run_id):
            raise ValueError('Identifiant du Reel invalide.')
        if not isinstance(published,bool):raise ValueError('État de publication invalide.')
        if metadata is not None:
            from urllib.parse import urlparse
            if not isinstance(metadata,dict) or set(metadata)-{'platform','url','published_at'}:raise ValueError('Informations de publication invalides.')
            if any(not isinstance(v,str) or len(v)>2000 for v in metadata.values()):raise ValueError('Informations de publication invalides.')
            url=metadata.get('url','')
            if url and (urlparse(url).scheme!='https' or not urlparse(url).hostname or urlparse(url).username):raise ValueError('Lien HTTPS de publication requis.')
            if metadata.get('published_at'):
                try:date=datetime.fromisoformat(metadata['published_at'])
                except ValueError:raise ValueError('Date de publication invalide.')
                if date.tzinfo is None or date>datetime.now(UTC):raise ValueError('Date de publication passée avec fuseau horaire requise.')
        self.store.root.mkdir(parents=True,exist_ok=True)
        # Share the feedback lock so an approval cannot change during archival.
        inbox=EditorialInbox(self.workflow,self.pipeline)
        inbox.store.root.mkdir(parents=True,exist_ok=True)
        with inbox.store.path('review.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            old=self.store.read(run_id+'.json')
            if old and old['published']==published and (metadata is None or old.get('metadata')==metadata):return old
            if not published and not old:raise ValueError('Ce Reel ne figure pas dans les archives.')
            if published:
                reviews=[r for r in inbox.list() if r['kind']=='run' and r['target_id']==run_id]
                if not reviews or reviews[-1]['decision']!='keep':
                    raise ValueError('Validez d’abord cette version finale du Reel.')
                run=inbox.target('run',run_id)
                if not run.get('watermark_free') or not self.pipeline.store.path(run['video']).is_file():
                    raise ValueError('Le fichier final du Reel est indisponible.')
            now=datetime.now(UTC).isoformat()
            if not old:
                project=self.pipeline.read(run['project_id']);place=run['place_id']
                snapshot={'run':run,'project':project,'dossier':self.workflow.dossier(place),
                          'reviews':[r for r in inbox.list() if r['place_id']==place],
                          'records':{}}
                # Preserve every place-specific JSON record, including old story versions.
                for path in self.workflow.store.root.rglob('*.json'):
                    if path.is_relative_to(self.store.root):continue
                    try:value=json.loads(path.read_text())
                    except (ValueError,OSError):continue
                    if isinstance(value,dict) and value.get('place_id')==place:
                        snapshot['records'][str(path.relative_to(self.workflow.store.root))]=value
                    elif isinstance(value,dict) and place in value:
                        snapshot['records'][str(path.relative_to(self.workflow.store.root))]={place:value[place]}
                from app.services.reel_covers import ReelCovers
                from app.services.quality_dossier import QualityDossier
                snapshot['quality']=QualityDossier(self.workflow,self.pipeline).build('run',run_id)
                snapshot['cover']=ReelCovers(self.workflow,self.pipeline).list().get(run_id)
                folder=self.store.path(run_id);folder.mkdir(exist_ok=True)
                self.store.write(run_id+'/data.json',snapshot)
                files={}
                def collect(value):
                    if isinstance(value,dict):
                        for v in value.values():collect(v)
                    elif isinstance(value,list):
                        for v in value:collect(v)
                    elif isinstance(value,str) and len(value)<1024:
                        for prefix,store in [('reels',self.pipeline.store),('workflow',self.workflow.store)]:
                            try:path=store.path(value)
                            except (ValueError,OSError):continue
                            if path.suffix.lower() in {'.mp4','.mov','.wav','.mp3','.m4a','.png','.jpg','.jpeg','.webp','.json','.txt','.srt','.html','.pdf'} and path.is_file() and not path.is_relative_to(self.store.root):
                                files[prefix+'/'+str(path.relative_to(store.root))]=path
                collect(snapshot)
                archive=folder/'archive.zip';temporary=folder/'archive.tmp'
                manifest={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in files.items()}
                with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_STORED) as bundle:
                    bundle.write(folder/'data.json','data.json')
                    bundle.writestr('manifest.json',json.dumps(manifest,indent=2))
                    for name,path in files.items():bundle.write(path,name)
                temporary.replace(archive)
                old={'run_id':run_id,'place_id':place,'snapshot':snapshot,
                     'archive_path':str(archive.relative_to(self.pipeline.store.root)),
                     'data_path':str((folder/'data.json').relative_to(self.pipeline.store.root)),
                     'events':[],'declared_by_owner':True,'platform_verified':False}
            if metadata is not None:old['metadata']=metadata
            old.update(published=published,updated_at=now)
            old['events'].append({'published':published,'at':now,'actor':self.workflow.read()['owner_id']})
            self.store.write(run_id+'.json',old)
            return old
