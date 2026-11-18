"""Compact cached review index; full evidence and archived snapshots are fetched on demand."""
import copy,json,threading
from datetime import datetime,UTC
from pathlib import Path
from app.services.studio_tasks import StudioTasks,digest
from app.services.editorial_inbox import EditorialInbox
from app.services.reel_story import StoryPreparation
from app.services.reel_publications import ReelPublications
from app.services.reel_covers import ReelCovers
from app.services.reel_jobs import ReelJobs
from app.services.reel_render_requests import ReelRenderRequests
from app.services.editorial_memory import memory_view


def fields(value,names):return {k:copy.deepcopy(value[k]) for k in names.split() if k in value}
def slim_draft(draft):
    return dict(fields(draft,'angle caption'),beats=[{'text':b['text']} for b in draft.get('beats',[])])
def slim_story(story):
    return dict(fields(story,'id place_id author created_at stale'),draft=slim_draft(story['draft']),audition=fields(story.get('audition',{}),'sha256 created_at status duration audio_path') or None)
def slim_run(run):
    result=fields(run,'id status video sha256 watermark_free technical approved_narration created_at rendered_at')
    if 'approved_narration' in result:result['approved_narration']=fields(result['approved_narration'],'sha256 story_id')
    result['inputs']={'draft':slim_draft(run.get('inputs',{}).get('draft',{}))}
    return result

class ReviewState:
    def __init__(self,workflow,pipeline):
        self.workflow,self.pipeline=workflow,pipeline;self.cached=None;self.revision=None;self.mutex=threading.Lock()
    def signature(self):
        state=self.workflow.read();repository=Path(state['repository'])
        roots=[self.workflow.store.root,repository/'brands/example']
        result=[]
        for root in roots:
            for path in root.rglob('*.json'):
                rel=path.relative_to(root)
                if path.name=='engine.json' or any(x in rel.parts for x in ('deliveries','numba-cache')):continue
                stat=path.stat();result.append((str(path),stat.st_mtime_ns,stat.st_size))
        reference=self.workflow.store.read('reference-analysis.json',{})
        for key in ('coverage_path','run_path'):
            if reference.get(key):
                path=Path(reference[key]);stat=path.stat() if path.is_file() else None
                result.append((str(path),stat.st_mtime_ns if stat else None,stat.st_size if stat else None))
        # Source revisions must invalidate stories even when no workflow JSON was touched.
        for place in state['places'].values():
            for source in place.get('sources',[]):
                path=repository/source['path']
                stat=path.stat() if path.exists() else None
                result.append((str(path),stat.st_mtime_ns if stat else None,stat.st_size if stat else None))
        return digest(sorted(result,key=lambda r:r[0]))
    def updates(self):
        return {'revision':self.signature(),'immediate_engine':StudioTasks(self.workflow).store.read('engine.json',{})}
    def summary(self):
        with self.mutex:
            revision=self.signature()
            if revision!=self.revision or self.cached is None:
                self.cached=self.build();self.revision=revision
            result=copy.deepcopy(self.cached);result.update(revision=revision,immediate_engine=StudioTasks(self.workflow).store.read('engine.json',{}));return result
    def build(self):
        state=self.workflow.read();stories=StoryPreparation(self.workflow)
        coverage=self.workflow.store.read('reference-coverage.json',{})
        config=self.workflow.store.read('reference-analysis.json',{})
        if config.get('coverage_path') and Path(config['coverage_path']).is_file():
            from tools.studio_references import coverage as read_coverage
            coverage=read_coverage(config['coverage_path'],config.get('prosody_path'))
            if config.get('run_path') and Path(config['run_path']).is_file():
                run=json.loads(Path(config['run_path']).read_text());coverage['run']=fields(run,'status current_shortcode new_records_finished elapsed_seconds')
                coverage['observed_at']=datetime.fromtimestamp(Path(config['coverage_path']).stat().st_mtime,UTC).isoformat()

        prepared=[slim_story(s) for place in state['places'] if (s:=stories.latest(place))]
        projects=[]
        for path in self.pipeline.store.root.glob('projects/*/project.json'):
            p=self.pipeline.read(path.parent.name)
            rendered=[r for r in p.get('runs',[]) if r['status']=='rendered']
            projects.append(dict(fields(p,'id place_id events'),runs=[slim_run(rendered[-1])] if rendered else []))
        pubs={}
        for ident,p in ReelPublications(self.workflow,self.pipeline).list().items():
            snap=p['snapshot']
            pubs[ident]=dict(fields(p,'run_id place_id published updated_at archive_path data_path declared_by_owner platform_verified metadata metrics'),
                snapshot={'run':slim_run(snap['run']),'project':fields(snap['project'],'id place_id'),'cover':None,'reviews':[]})
        from app.services.studio_performance import performance_view
        for publication in pubs.values():
            publication['performance']=performance_view(publication)
        reviews=EditorialInbox(self.workflow,self.pipeline).list()
        return {'prepared_stories':prepared,'projects':projects,'publications':pubs,'dossiers':[{'place':fields(p,'id name owner_interest')} for p in state['places'].values()],
                'inbox_reviews':[fields(r,'id kind target_id place_id decision audition_sha256 created_at') for r in reviews],
                'immediate_tasks':[fields(t,'id place_id origin request_type chain_version status attempts message created_at received_at started_at updated_at delivered_at retry_at review_ids proof transport_verified revision_progress') for t in StudioTasks(self.workflow).list()],
                'render_requests':[fields(r,'id story_id audition_sha256 status') for r in ReelRenderRequests(self.workflow).list()],
                'jobs':[fields(j,'id place_id story_id kind status finished_at') for j in ReelJobs(self.workflow,self.pipeline).list()],
                'worker':ReelJobs(self.workflow,self.pipeline).status(),'music_plans':{},'visual_audits':{},'covers':{},'feedback_receipts':{},'editorial_memory':{},
                'operating':self.workflow.store.read('studio-operating.json',{'active_limit':3,'priority_places':[]}),
                'benchmark_insights':[json.loads(path.read_text()) for path in (self.workflow.store.root/'benchmark-insights').glob('*.json')],
                'reference_coverage':coverage,'release':self.workflow.store.read('release.json',{})}
    def detail(self,kind,ident):
        pub=ReelPublications(self.workflow,self.pipeline).store.read(ident+'.json') if kind=='run' else None
        if pub:
            from app.services.studio_performance import performance_view
            pub=dict(pub,performance=performance_view(pub))
        if pub and pub['published']:
            snap=pub['snapshot'];place=pub['place_id']
            visual=next((v[place] for k,v in snap.get('records',{}).items() if k.endswith('visual-audits.json') and place in v),None)
            latest_cover=ReelCovers(self.workflow,self.pipeline).list().get(ident)
            archived_cover=snap.get('cover') or {}
            revised_cover=latest_cover if latest_cover and latest_cover.get('status')=='ready' and latest_cover.get('identity')!=archived_cover.get('identity') else None
            return {'revised_cover':revised_cover,'publication':pub,'target':snap['run'],'project':snap['project'],'dossier':snap['dossier'],'reviews':snap['reviews'],
                    'cover':snap.get('cover'),'visual_audit':visual,'quality':snap.get('quality'),'archived':True}
        target=EditorialInbox(self.workflow,self.pipeline).target(kind,ident);place=target['place_id']
        from app.services.editorial_server import music_plans_for_review
        from app.services.quality_dossier import QualityDossier
        reviews=[r for r in EditorialInbox(self.workflow,self.pipeline).list() if r['place_id']==place]
        from app.services.editorial_memory import feedback_interpretation
        overrides=self.workflow.store.read('editorial-memory/overrides.json',{})
        for r in reviews:
            override=overrides.get(r['id'],{})
            r['effective_memory_active']=override.get('active',True)
            r['effective_interpretation']=feedback_interpretation(r.get('note',''),override.get('scope',r.get('memory_scope','auto')))
        previous=[]
        if kind=='story':
            for path in self.pipeline.store.root.glob('story-proposals/*/auditions/*/audition.json'):
                a=json.loads(path.read_text());storyfile=path.parents[2].with_suffix('.json')
                if a.get('audio_path') and storyfile.exists() and json.loads(storyfile.read_text()).get('place_id')==place and a.get('sha256')!=target.get('audition',{}).get('sha256'):
                    previous.append(fields(a,'audio_path sha256 created_at duration'))
        from app.services.production_chain import ProductionChain
        return {'production_chain':ProductionChain(self.workflow).detail(place),
                'target':target,'project':self.pipeline.read(target['project_id']) if kind=='run' else None,
                'dossier':self.workflow.dossier(place),'reviews':reviews,'music_plan':music_plans_for_review(self.workflow).get(place),
                'visual_audit':self.workflow.store.read('visual-audits.json',{}).get(place),
                'cover':ReelCovers(self.workflow,self.pipeline).list().get(ident),'editorial_memory':memory_view(self.workflow,place),
                'feedback_receipts':self.workflow.store.read('feedback-receipts.json',{}),'quality':QualityDossier(self.workflow,self.pipeline).build(kind,ident),
                'previous_audition':sorted(previous,key=lambda a:a.get('created_at',''))[-1] if previous else None,'archived':False}
