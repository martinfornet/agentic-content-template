"""Local event-driven production executor. Stages work and fences delivery by review/version."""
import argparse, fcntl, hashlib, json, os, re, shutil, signal, subprocess, time
from pathlib import Path
from app.services.editorial_workflow import EditorialWorkflow
from app.services.studio_tasks import StudioTasks, digest, now

SCHEMA={'type':'object','additionalProperties':False,'properties':{'status':{'type':'string','enum':['ready','blocked']},'message':{'type':'string'},'kind':{'type':'string','enum':['story','run']},'output_id':{'type':'string'}},'required':['status','message','kind','output_id']}

def filehash(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def same_inputs(a,b):
    # Global feedback from a different place must not invalidate this owner's request.
    # The version of the charter and this place's memory remain fenced independently.
    return digest({k:v for k,v in a.items() if k!='memory_version'})==digest({k:v for k,v in b.items() if k!='memory_version'})

UNPUBLISHED_BATCH_MARKER = '[CONTENT-UNPUBLISHED-BATCH]'

def published_batch_excluded(tasks, task):
    """The owner's unpublished-only batch cannot process a declared published place."""
    if task.get('origin') != 'chat' or UNPUBLISHED_BATCH_MARKER not in task.get('review',{}).get('note',''):
        return False
    root=tasks.workflow.store.root/'reels/published'
    for path in root.glob('*.json'):
        record=json.loads(path.read_text())
        if record.get('place_id')==task['place_id'] and record.get('published') is True:
            return True
    return False

def requires_new_voice(task):
    from app.services.revision_contract import scope_for
    return task['review']['kind']=='story' and not scope_for(task)['preserve_voice']

def valid_current(task,snapshot):
    if task.get('request_type')=='production':
        return task['input_version']==digest({k:v for k,v in snapshot.items() if k!='memory_version'})
    reviews=snapshot['reviews']
    if not reviews or reviews[-1]['id']!=task['review_ids'][-1]:return False
    r=task['review'];s=snapshot['story']
    if r['kind']=='story':
        expected=r.get('new_story_id') if r['decision']=='edited' else r['target_id']
        return bool(s and s['id']==expected and (not s.get('stale') or s.get('additive_sources_only') is True) and (r['decision']=='edited' or r.get('audition_sha256')==s.get('audition',{}).get('sha256')))
    return any(any(x['id']==r['target_id'] for x in p['runs']) for p in snapshot['projects'])

def prompt_for(task, stage, base, config):
    """Build a brand-neutral, source-grounded execution brief for a staged task."""
    from app.services.production_chain import chain_brief
    from app.services.revision_contract import scope_for
    return chain_brief(None) + f'''You are the content production worker for this workspace.
Follow the task scope and configured brand direction. This is a production task,
not a code-editing task. Work only in the supplied stage; treat other paths as
read-only. Do not publish, upload, purchase, contact external services, or
change application code, configuration, or approved source material.

Read the staged brief, source evidence, current versions, review history, and
revision contract. Make only the requested change. Preserve previously approved
material outside scope. Every factual claim must point to its source. Never
invent source evidence, owner feedback, a listening/viewing check, an approval,
or a performance result. A changed artifact is a new unapproved proposal until
a person reviews that exact version.

For voice work, follow the configured voice engine and pronunciation settings.
Keep the transcript, generated audio, settings, seed where applicable, and file
hash associated with the same candidate. Voice validation requires an actual
listen by a person; transcript and signal measurements are technical evidence,
not a substitute for listening. State any checks that were not performed.

For video work, preserve factual provenance for each asset and record the exact
export, version, and hash. Run the configured technical checks, then request
independent human review. Fix findings and review the changed version again.

Task: {json.dumps(task, ensure_ascii=False)}
Scope: {json.dumps(scope_for(task), ensure_ascii=False)}
Stage: {stage}
Workspace: {stage.parent}
'''

def validate_output_route(task,result):
    # A Reel correction/voice approval must yield a video, never send its owner
    # back through a standalone voice approval as an implicit fallback.
    review=task['review']
    if task.get('request_type')=='production' and result['kind']!='story':
        raise ValueError('Une nouvelle production attend d’abord la validation du vocal, pas un Reel automatique.')
    if (review['kind']=='run' or review['decision']=='keep') and result['kind']!='run':
        raise ValueError('Un Reel corrigé est attendu : une livraison audio seule ne termine pas cette demande.')
    if review['kind']=='run' and result['output_id']==review['target_id']:
        raise ValueError('La correction doit livrer une nouvelle version du Reel.')

def validate_output(task,stage,result,before):
    from app.services.reel_story import StoryPreparation,place_packet,review_story
    from app.services.reel_pipeline import ReelPipeline
    from app.services.editorial_inbox import EditorialInbox
    from app.services.reel_audio import measure_audio
    validate_output_route(task,result)
    w=EditorialWorkflow(stage/'output/example/editorial-workflow');p=ReelPipeline(w)
    # The executor must not manufacture owner feedback or change factual evidence.
    reviews=[r for r in EditorialInbox(w,p).list() if r['place_id']==task['place_id']]
    if reviews!=before['reviews']:raise ValueError('Le moteur a modifié les retours propriétaire : livraison refusée.')
    s=StoryPreparation(w).latest(task['place_id'])
    if not s or s.get('stale') or review_story(place_packet(w,task['place_id']),s['draft'])['reference_errors']:raise ValueError('Récit ou sources incohérents.')
    plans=w.store.read('music-plans.json',{})
    voice_review=None
    if result['kind']=='story':
        if s['id']!=(before['story'] or {}).get('id') and s.get('author')!='assistant':raise ValueError('Une proposition assistant ne peut pas créer une correction propriétaire.')
        if s['id']!=result['output_id']:raise ValueError('La version livrée n’est pas le récit courant préparé.')
        a=s.get('audition',{})
        if a.get('status')!='ready_to_listen' or not a.get('alignment_passed'):raise ValueError('Voix ou alignement non conformes.')
        visual=w.store.read('visual-audits.json',{}).get(task['place_id'])
        old_audio=(before['story'] or {}).get('audition',{}).get('sha256')
        if requires_new_voice(task) and a.get('sha256')==old_audio:raise ValueError('La correction vocale conserve le même audio : livraison refusée.')
        old_mixes={c.get('sha256') for c in (before.get('music') or {}).get('candidates',[])}
        new_mixes={c.get('sha256') for c in plans.get(task['place_id'],{}).get('candidates',[])}
        if s['id']==(before['story'] or {}).get('id') and a.get('sha256')==old_audio and new_mixes==old_mixes and visual==before['visuals']:raise ValueError('Aucune correction livrée.')
        if s['draft']!=(before['story'] or {}).get('draft') and s.get('author')=='assistant':
            from app.services.generation_quality import validate_direction
            validate_direction(place_packet(w,task['place_id']),s['draft'])
        if a.get('sha256')!=old_audio:
            from app.services.generation_quality import selection_report
            voice_review=selection_report(p,s)
        if filehash(p.store.path(a['audio_path']))!=a['sha256'] or not measure_audio(p.store.path(a['audio_path']))['passed']:raise ValueError('Contrôle audio indépendant refusé.')
        choices=plans.get(task['place_id'],{}).get('candidates',[])
        if len(choices)<2:raise ValueError('Les deux choix musicaux sont requis.')
        from app.services.reel_music_usage import unavailable
        for c in choices:
            blocked=unavailable(w,c,task['place_id'])
            if blocked:raise ValueError(blocked)
            if c['story_id']!=s['id'] or c['voice_sha256']!=a['sha256'] or not c.get('qa_passed') or filehash(p.store.path(c['audio_path']))!=c['sha256']:raise ValueError('Choix musical incohérent.')
            if not measure_audio(p.store.path(c['audio_path']))['passed']:raise ValueError('Mix musical non conforme.')
        paths=[f for f in (p.store.root/'story-proposals'/s['id']).rglob('*') if f.is_file()]
        paths += [p.store.path(c['audio_path']) for c in choices]
        # New sourced visual previews may accompany a voice or music-only correction.
        def visual_files(value):
            if isinstance(value,dict):
                for item in value.values():visual_files(item)
            elif isinstance(value,list):
                for item in value:visual_files(item)
            elif isinstance(value,str) and not value.startswith(('/', 'http')) and Path(value).suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
                candidate=p.store.path(value)
                if candidate.is_file():paths.append(candidate)
        if visual!=before['visuals']:visual_files(visual)
        # Make the new story visible last, once audio and both choices are already copied.
        paths += [p.store.path('story-proposals/'+s['id']+'.json')]
        proof={'story_id':s['id'],'voice_sha256':a['sha256'],'audio_path':a['audio_path'],'music_sha256':[c['sha256'] for c in choices]}
    else:
        projects=[p.read(x.parent.name) for x in p.store.root.glob('projects/*/project.json')]
        matches=[(pr,run) for pr in projects if pr['place_id']==task['place_id'] for run in pr['runs'] if run['id']==result['output_id']]
        if len(matches)!=1:raise ValueError('Rendu livré introuvable.')
        pr,run=matches[0]
        if run['status']!='rendered' or not run.get('technical',{}).get('passed') or not run.get('watermark_free'):raise ValueError('Le rendu doit être terminé, contrôlé et sans bandeau.')
        if task['review']['decision']=='keep':
            proof=run.get('approved_narration') or run.get('inputs',{}).get('approved_narration') or pr.get('approved_narration') or {}
            if proof.get('sha256')!=task['review'].get('audition_sha256'):raise ValueError('Le rendu ne prouve pas la réutilisation de la voix validée.')
            if proof.get('music',{}).get('sha256')!=task['review'].get('music',{}).get('sha256'):raise ValueError('Le rendu ne prouve pas la musique choisie.')
        old_projects={x['id']:x for x in before['projects']}
        old=old_projects.get(pr['id'],{})
        for previous in old.get('runs',[]):
            if previous not in pr['runs']:raise ValueError('Ancien rendu ou décision modifié : livraison refusée.')
        if pr.get('approval')!=old.get('approval') or pr.get('visual_lot_approval')!=old.get('visual_lot_approval'):raise ValueError('Nouvelle approbation propriétaire interdite.')
        video=p.store.path(run['video'])
        if not video.is_file():raise ValueError('Vidéo absente.')
        # Validate independently using the same complete QA used by the renderer where available.
        if not measure_audio(video)['passed']:raise ValueError('Audio du Reel non conforme.')
        independent=p.technical_qa(video,run['technical']['duration'],stage.parent/'delivery-video-qa.log')
        if not independent['passed']:raise ValueError('Contrôle vidéo indépendant refusé.')
        if filehash(video)!=run['sha256']:raise ValueError('Empreinte du Reel incorrecte.')
        from app.services.visual_quality import verify_run
        from app.services.reel_rhythm import verify_revision
        if task['review']['kind']=='run':
            previous=next((x for x in old.get('runs',[]) if x['id']==task['review']['target_id']),None)
            if previous is None:raise ValueError('Montage de référence absent du reçu initial.')
            from app.services.revision_contract import scope_for
            preserve_cuts=scope_for(task)['preserve_cuts']
            verify_revision(old,previous,pr,run,preserve_cuts=preserve_cuts)
        new_voice=run.get('approved_narration') or run.get('inputs',{}).get('approved_narration') or pr.get('approved_narration') or {}
        if task['review']['kind']=='run':
            previous=next(x for x in old.get('runs',[]) if x['id']==task['review']['target_id'])
            old_voice=previous.get('approved_narration') or previous.get('inputs',{}).get('approved_narration') or old.get('approved_narration') or {}
            from app.services.revision_contract import scope_for, protect_audio
            from app.services.revision_active import active_revision
            active = active_revision(pr, task['review'])
            validated_fallback = None
            if active == 'revision_voice_proposal':
                from app.services.reel_revision_voice import removed_beat_candidate
                validated_fallback = removed_beat_candidate(p, pr, task['review'])
                if validated_fallback['narration'] != new_voice or validated_fallback['draft'] != run['inputs']['draft']:
                    raise ValueError('Le rendu ne correspond pas à la proposition de secours vérifiée.')
                if review_story(place_packet(w,task['place_id']),validated_fallback['draft'])['reference_errors']:
                    raise ValueError('Le récit corrigé présente des références incohérentes.')
            validated_story = None
            if active == 'revision_story_proposal':
                from app.services.reel_revision_story import story_candidate
                validated_story = story_candidate(p, pr, task['review'])
                if validated_story['narration'] != new_voice or validated_story['draft'] != run['inputs']['draft']:
                    raise ValueError('Le rendu ne correspond pas au récit de correction vérifié.')
            if active == 'revision_mix_proposal':
                from app.services.reel_revision_mix import mix_candidate
                candidate=mix_candidate(p,pr,task['review'])
                if candidate['narration']!=new_voice or candidate['draft']!=run['inputs']['draft']:
                    raise ValueError('Le rendu ne correspond pas au mix corrigé vérifié.')
            checked_voice = dict(new_voice, music=old_voice.get('music')) if (validated_fallback or validated_story) else new_voice
            protect_audio(scope_for(task),old_voice,checked_voice)
            if new_voice.get('sha256') and new_voice.get('sha256')!=old_voice.get('sha256'):
                from app.services.generation_quality import selection_report
                if validated_fallback:
                    pass  # Exact PCM splice and bound original mix verified above.
                elif s.get('audition',{}).get('sha256')!=new_voice['sha256']:
                    raise ValueError('La voix du Reel corrigé ne correspond pas à la comparaison.')
                if not validated_fallback:
                    voice_review=selection_report(p,s)
        from app.services.visual_reuse import require_task_binding
        require_task_binding(task,run)
        visual_quality=verify_run(p,pr,run,video=video)
        from app.services.generation_quality import montage_report
        artistic=montage_report(p,run)
        paths=[f for f in (p.store.root/'projects'/pr['id']).rglob('*') if f.is_file() and f.name!='project.json']
        if video not in paths:paths.append(video)
        for asset in run['inputs']['assets']:
            if asset is None:continue
            paths.append(p.store.path(asset['file_path']))
            source=asset['metadata']['source']
            paths.append(p.store.path('sources/'+source['source_id']+'.json'))
        for shot in artistic['shots']:
            paths.extend(p.store.path(c['file_path']) for c in shot['candidates'])
        paths.append(p.store.path(run['artistic_review_path']))
        paths=list(dict.fromkeys(paths))
        paths.append(p.store.path('projects/'+pr['id']+'/project.json'))
        proof={'run_id':run['id'],'video':run['video'],'sha256':filehash(video),'project_id':pr['id'],'visual_quality':visual_quality,'artistic_review_path':run['artistic_review_path'],'artistic_review_sha256':run['artistic_review_sha256']}
    if voice_review:
        paths.extend(p.store.path(c['file_path']) for c in voice_review['candidates'])
        paths.append(p.store.path(s['audition']['selection_path']))
        if result['kind']=='run':
            paths.extend(f for f in p.store.path('story-proposals/'+s['id']).rglob('*') if f.is_file())
            paths.append(p.store.path('story-proposals/'+s['id']+'.json'))
        paths=list(dict.fromkeys(paths))
    if task.get('request_type')=='production':
        from app.services.photo_preflight import require_current
        photos=require_current(p,task['place_id'])
        paths.append(p.store.path('photo-preflights/'+task['place_id']+'/'+photos['id']+'.json'))
        for asset in photos['manifest']['assets']:
            paths.append(p.store.path(asset['file_path']))
            paths.append(p.store.path('sources/'+asset['metadata']['source']['source_id']+'.json'))
        proof['photo_preflight_id']=photos['id']
    paths=list(dict.fromkeys(paths))
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(p.store.root):raise ValueError('Fichier hors espace autorisé.')
    return paths,plans.get(task['place_id']),proof

def publish(tasks,task,stage,result,before,critic=None):
    tasks.update(task['id'],status='verifying',message='Contrôle des fichiers avant livraison.')
    paths,plan,proof=validate_output(task,stage,result,before)
    live=tasks.workflow.store.root;staged=stage/'output/example/editorial-workflow'
    lockfile=live/'reels/inbox/review.lock';lockfile.parent.mkdir(parents=True,exist_ok=True)
    with lockfile.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if published_batch_excluded(tasks,task):raise ValueError('Lieu déclaré publié pendant la production : livraison du lot annulée.')
        if not same_inputs(before,tasks.snapshot(task['place_id'])):raise ValueError('Une nouvelle version ou un nouveau retour est arrivé : résultat conservé sans écrasement.')
        if task.get('chain_version'):
            from app.services.studio_critic import validate_report,verify_files
            if not critic or critic['receipt']['reviewer']!='separate_codex_read_only':
                raise ValueError('Revue indépendante obligatoire avant livraison.')
            packet=critic['packet'];report=critic['receipt']['report']
            validate_report(packet,report);verify_files(packet)
            if report['verdict']!='pass' or packet['proof']!=proof or packet['task_id']!=task['id']:
                raise ValueError('La revue indépendante ne permet pas la livraison de cette version.')
            expected={str(p.resolve()):filehash(p) for p in paths}
            if expected!={r['path']:r['sha256'] for r in packet['files']}:
                raise ValueError('Le lot a changé après la revue indépendante.')
        from app.services.reel_music_usage import unavailable, record_delivered
        if result['kind']=='story':
            for candidate in (plan or {}).get('candidates',[]):
                blocked=unavailable(tasks.workflow,candidate,task['place_id'])
                if blocked:raise ValueError(blocked)
        else:
            staged_project=json.loads((staged/'reels/projects'/proof['project_id']/'project.json').read_text())
            run=next(r for r in staged_project['runs'] if r['id']==proof['run_id'])
            music=(run.get('approved_narration') or run.get('inputs',{}).get('approved_narration') or {}).get('music') or run.get('music')
            if music:
                blocked=unavailable(tasks.workflow,music,task['place_id'],existing_reel=task['review']['kind']=='run')
                if blocked:raise ValueError(blocked)
        from app.services.studio_delivery import commit_files,delivery_lock
        from app.services.acquisition_store import AcquisitionStore
        package=AcquisitionStore(stage.parent/'delivery-package')
        files={str(source.relative_to(staged)):source for source in paths}
        for relative,source in files.items():
            target=live/relative
            if target.exists() and filehash(target)!=filehash(source) and target.name!='project.json':
                raise ValueError('Livraison refusée : écrasement d’une version existante.')
        if plan:
            plans=tasks.workflow.store.read('music-plans.json',{});plans[task['place_id']]=plan
            package.write('music-plans.json',plans);files['music-plans.json']=package.path('music-plans.json')
        visual=EditorialWorkflow(staged).store.read('visual-audits.json',{}).get(task['place_id'])
        if visual!=before['visuals']:
            audits=tasks.workflow.store.read('visual-audits.json',{});audits[task['place_id']]=visual
            package.write('visual-audits.json',audits);files['visual-audits.json']=package.path('visual-audits.json')
        if result['kind']=='run' and music:
            from app.services.reel_music_usage import usage,track_key
            rows=usage(tasks.workflow)
            if not any(r.get('run_id')==proof['run_id'] for r in rows):
                rows.append(dict(key=track_key(music),title=music['title'],artist=music.get('artist',''),place_id=task['place_id'],run_id=proof['run_id'],reason='integrated_in_delivered_reel'))
            package.write('music-usage.json',{'policy':'no_reproposal_after_delivered_reel','used':rows})
            files['music-usage.json']=package.path('music-usage.json')
        from app.services.editorial_memory import active_rules
        from contextlib import nullcontext
        from app.services.production_chain import ProductionChain
        chain=ProductionChain(tasks.workflow)
        if critic:
            package.write('critic.json',critic['receipt'])
            files['immediate-tasks/'+task['id']+'/critic.json']=package.path('critic.json')
        with chain.lock() if critic else nullcontext():
            if critic:
                relative,document=chain.delivery_document(task['id'],proof,critic['receipt'])
                package.write('chain-delivery.json',document);files[relative]=package.path('chain-delivery.json')
            receipt={'proof':proof,'files':[{'path':r,'sha256':filehash(p)} for r,p in files.items()],
                     'charter_version':digest(active_rules(tasks.workflow)),'source_version':digest(before),'delivered_at':now()}
            package.write('delivery.json',receipt)
            files['immediate-tasks/'+task['id']+'/delivery.json']=package.path('delivery.json')
            with delivery_lock(tasks.workflow,True):manifest=commit_files(tasks.workflow,task['id'],files)
        # The producer reports before the separate review. Its staging summary may
        # still say that review is pending, so expose the completed delivery state.
        message=result['message']
        if critic:
            message='Nouvelle version prête à examiner. Revue indépendante terminée. '+critic['receipt']['report']['summary']
        tasks.update(task['id'],status='delivered',delivered_at=now(),message=message,producer_message=result['message'],proof=proof)
    # The visible transaction is already complete; a transport outage must not cause regeneration.
    import urllib.request,urllib.parse
    served=[]
    try:
        for row in manifest:
            relative=Path(row['path'])
            if relative.suffix.lower() not in {'.wav','.mp3','.mp4','.jpg','.jpeg','.png','.webp'}:continue
            file_path=str(relative.relative_to('reels'))
            with urllib.request.urlopen('http://127.0.0.1:8769/files?path='+urllib.parse.quote(file_path),timeout=90) as response:
                actual=hashlib.sha256(response.read()).hexdigest()
            if actual!=row['sha256']:raise ValueError('Le fichier servi diffère du résultat contrôlé.')
            served.append(file_path)
        tasks.update(task['id'],served_files_verified=served,transport_verified=True)
    except (OSError,ValueError) as error:
        tasks.update(task['id'],transport_verified=False,transport_error=str(error)[:400])
    return proof

def execute(tasks,task,config):
    if published_batch_excluded(tasks,task):
        tasks.update(task['id'],status='superseded',message='Sujet déclaré publié : exclu du lot demandé.')
        return
    before=tasks.snapshot(task['place_id'])
    if not valid_current(task,before):
        tasks.update(task['id'],status='superseded',message='Ce retour concerne une version remplacée.');return
    root=Path(config['work_root'])/task['id'];root.mkdir(parents=True,exist_ok=True)
    basefile=root/'base-snapshot.json'
    stage=root/'stage/repository'
    if stage.exists():
        previous=json.loads(basefile.read_text()) if basefile.exists() else None
        if previous is None:raise ValueError('Essai sans état de départ : fichiers conservés pour diagnostic.')
        migrated='production_memory_version' not in previous or 'place_id' not in previous
        previous.setdefault('place_id',task['place_id'])
        if 'production_memory_version' not in previous:
            from app.services.editorial_memory import production_memory_version
            previous['production_memory_version']=production_memory_version(EditorialWorkflow(stage/'output/example/editorial-workflow'),task['place_id'])
        if not same_inputs(previous,before):
            tasks.restart_current_context(task['id'])
            return
        if migrated:
            shutil.copy2(basefile,root/'base-snapshot.before-scoped-memory.json')
            basefile.write_text(json.dumps(previous,ensure_ascii=False,indent=2))
        # Preserve the exact memory used for this attempt and its delivery receipt.
        before=previous
        # Refresh code and active brand guidance, preserving all staged production artifacts.
        for name in ('app','brands'):
            shutil.copytree(Path(config['studio_repository'])/name,stage/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for name in ('GENERATION_QUALITY.md','BRAND_DIRECTION.md','PRODUCTION_CHAIN.md'):
            source=Path(config['studio_repository'])/'docs'/name
            if source.is_file():
                (stage/'docs').mkdir(exist_ok=True)
                shutil.copy2(source,stage/'docs'/name)
        # A finished producer output must still pass the separate critic on resume.
        for name in ('engine.log','result.json','progress.json'):
            old=root/name
            if old.exists():shutil.copy2(old,root/(name+'.attempt-'+str(task['attempts']-1)))
    else:
        basefile.write_text(json.dumps(before,ensure_ascii=False,indent=2))
        from app.services.studio_stage import prepare_stage
        stats=prepare_stage(config['studio_repository'],stage,before)
        tasks.update(task['id'],stage_copy=stats)
    w=stage/'output/example/editorial-workflow/workflow.json';v=json.loads(w.read_text());v['repository']=str(stage);w.write_text(json.dumps(v,ensure_ascii=False,indent=2))
    # Pin the approved local voice method and alignment models for every future montage.
    settings=stage/'brands/example/reel.json'
    cfg=json.loads(settings.read_text());refs=Path(config['references'])
    # Voice engine, reference, checkpoint, and alignment paths must come from this project's local config.
    cfg.setdefault('narration', {})
    if not cfg.get('voice_configuration'):
        raise ValueError('Configure the voice engine and private local assets before production.')
    cfg.update(photo_motion=cfg.get('photo_motion', {'enabled': False}))
    settings.write_text(json.dumps(cfg,indent=2))
    complete_chain(tasks,task,config,stage,root,before)


def current_progress(root, since):
    """Ignore prior attempts and prefer the newest producer progress file."""
    paths = [p for p in (root/'progress.json', root/'stage/progress.json')
             if p.is_file() and p.stat().st_mtime >= since]
    for path in sorted(paths, key=lambda p:p.stat().st_mtime, reverse=True):
        try:
            message = json.loads(path.read_text()).get('message', '')
            if isinstance(message, str) and message.strip():
                return message[:400]
        except (ValueError, OSError):
            pass
    return ''


def produce(tasks,task,config,stage,root,before):
    (root/'schema.json').write_text(json.dumps(SCHEMA))
    # Require account authentication. Never switch to billed API-key credentials.
    env=dict(os.environ)
    for key in ['OPENAI_API_KEY','CODEX_API_KEY','SSH_AUTH_SOCK','SSH_AGENT_PID']:env.pop(key,None)
    # Local audio analysis must keep its compiled cache inside this production's
    # writable workspace, including when spawned Python runtimes differ.
    cache=root/'stage/numba-cache';cache.mkdir(parents=True,exist_ok=True)
    env['NUMBA_CACHE_DIR']=str(cache)
    status=subprocess.run([config['codex'],'login','status'],env=env,text=True,capture_output=True,timeout=30)
    if status.returncode or 'ChatGPT' not in status.stdout+status.stderr:raise ValueError('Connexion Codex par compte ChatGPT requise ; aucun service API payant ne sera utilisé.')
    cmd=[config['codex'],'exec','--ignore-user-config','--ephemeral','--approve-for-me','--cd',str(root),'--skip-git-repo-check','--output-schema',str(root/'schema.json'),'-o',str(root/'result.json'),'-']
    with (root/'engine.log').open('w') as log:
        proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=log,stderr=log,text=True,env=env,start_new_session=True)
        tasks.update(task['id'],executor_pid=proc.pid,message='Moteur de correction lancé ; analyse du texte et des sources.')
        proc.stdin.write(prompt_for(task,stage,before,config));proc.stdin.close();started=time.time()
        try:
            while proc.poll() is None:
                tasks.store.write('engine.json',{'online_at':now(),'pid':os.getpid(),'active_task':task['id']})
                msg=current_progress(root,started)
                if msg:tasks.update(task['id'],status='producing',message=msg)
                if time.time()-started>config.get('timeout_seconds',5400):raise TimeoutError('Délai dépassé ; les fichiers sont conservés pour une reprise contrôlée.')
                # Later owner feedback fences the result and cancels the obsolete attempt.
                if not same_inputs(before,tasks.snapshot(task['place_id'])):raise ValueError('Nouveau retour reçu : essai précédent conservé et remplacé.')
                time.sleep(2)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
    if proc.returncode:
        tail=(root/'engine.log').read_text(errors='replace')[-12000:].lower()
        if any(term in tail for term in ('dns','connection reset','network is unreachable','failed to connect','stream disconnected')):
            raise ConnectionError('Connexion au moteur interrompue ; essai conservé.')
        raise ValueError('Le moteur a interrompu le traitement. Consulter le journal local privé.')
    result=json.loads((root/'result.json').read_text())
    if result['status']!='ready':raise ValueError(result['message'])
    return result

def complete_chain(tasks,task,config,stage,root,before):
    from app.services.production_chain import ProductionChain,VERSION
    from app.services.studio_critic import prepare_packet,run_review,recover_completed_reviews
    from app.services.revision_contract import scope_for
    from app.services.revision_progress import RevisionProgress,validate_plan
    chain=ProductionChain(tasks.workflow)
    task=tasks.update(task['id'],chain_version=VERSION)
    frozen=chain.begin(task,before)
    recorded={e['evidence'].get('packet_sha256') for e in chain.events(task['id']) if e['step']=='independent_review'}
    for receipt in recover_completed_reviews(root,recorded):
        if receipt['packet_sha256'] not in recorded:
            chain.event(task['id'],'independent_review',receipt)
    relatives=[r for r in chain.records(task['place_id']) if r['request']['id']==task['review']['id']]
    ledger=RevisionProgress(root,[e for r in relatives for e in chain.events(r['task_id'])],
                            journal=root.parent/'revision-journals'/task['review']['id'],
                            history_roots=[root.parent/r['task_id'] for r in relatives])
    frozen=dict(frozen,previous_workspaces=[{'path':str(root.parent/r['task_id']/'stage/repository'),'read_only':True}
                                          for r in relatives if r['task_id']!=task['id']])
    ledger.allow_manual_retry(task.get('retry_token'))
    scope=scope_for(task)
    staged=EditorialWorkflow(stage/'output/example/editorial-workflow')
    completed=root/'result.json'
    result=json.loads(completed.read_text()) if completed.exists() else None

    def progress(phase,contract,**extra):
        value={'phase':phase,'review_count':len(ledger.state['reviews']),'review_limit':ledger.state['limit'],
               'remaining':[i['required_change'] for i in contract['issues'] if i['severity']=='blocking'],
               'preserve_voice':scope['preserve_voice'],'preserve_music':scope['preserve_music'],**extra}
        tasks.update(task['id'],revision_progress=value)

    def archive_result():
        if completed.exists():
            completed.rename(root/f'result.before-repair-{time.time_ns()}.json')
        if (root/'engine.log').exists():
            shutil.copy2(root/'engine.log',root/f'engine.before-repair-{time.time_ns()}.log')

    accepted=ledger.passed_receipt() if result and result.get('status')=='ready' else None
    if accepted:
        _,_,proof=validate_output(task,stage,result,before)
        if (proof.get('voice_sha256') if result['kind']=='story' else proof.get('sha256'))!=ledger.latest['facts']['media']:
            raise ValueError('Le résultat ne correspond plus au fichier accepté par la revue.')
        published=publish(tasks,task,stage,result,before,critic=accepted)
        progress('delivered',ledger.contract(task,scope))
        return published

    while True:
        context=ledger.context(frozen,scope)
        contract=ledger.contract(task,scope)
        (root/'production-context.json').write_text(json.dumps(context,ensure_ascii=False,indent=2))
        staged.store.write('production-context.json',context)
        staged.store.write('revision-contract.json',contract)
        latest=ledger.latest
        if latest and latest['report']['verdict']!='pass':
            (root/'critique-feedback.json').write_text(json.dumps(latest['report'],ensure_ascii=False,indent=2))
        # A prior ready result can predate the current contract even without a
        # critic receipt. Archive it and run the producer with a fresh plan.
        if result and result.get('status')=='ready':
            try: validate_plan(contract,staged.store.read('revision-plan.json'))
            except ValueError:
                archive_result();result=None
        progress('repair' if latest else 'preparation',contract)
        ledger.require_budget()
        if not result or result.get('status')!='ready':
            tasks.update(task['id'],status='producing',message='Correction ciblée des points restants.' if latest else 'Préparation de la proposition.')
            result=produce(tasks,task,config,stage,root,before)
        plan=validate_plan(contract,staged.store.read('revision-plan.json'))
        paths,music,proof=validate_output(task,stage,result,before)
        from app.services.reel_story import StoryPreparation
        story=StoryPreparation(staged).latest(task['place_id'])
        chain.event(task['id'],'direction',{'story_id':story['id'],'generation':story['draft'].get('generation')})
        if proof.get('photo_preflight_id'):
            chain.event(task['id'],'photo_preflight',{'id':proof['photo_preflight_id'],'passed':True})
        chain.event(task['id'],'voice' if result['kind']=='story' else 'montage',{'proof':proof})
        folder=root/'critic'/f'attempt-{task["attempts"]}-round-{len(ledger.state["reviews"])}'
        packet=prepare_packet(task,stage,result,proof,paths,folder,context)
        evidence=ledger.check_candidate(packet,plan)
        packet.update(revision_plan=plan,repair_evidence=evidence)
        progress('review',contract,last_changes=plan['owner_request_response'])
        tasks.update(task['id'],status='verifying',message=f'Revue indépendante {len(ledger.state["reviews"])+1}/{ledger.state["limit"]} : vérification des corrections apportées.')
        def tick():
            tasks.store.write('engine.json',{'online_at':now(),'pid':os.getpid(),'active_task':task['id']})
            if not same_inputs(before,tasks.snapshot(task['place_id'])):
                raise ValueError('Nouveau retour reçu pendant la revue : version conservée sans livraison.')
        receipt=run_review(packet,folder,config,tick=tick,
                           on_start=lambda pid:tasks.update(task['id'],executor_pid=pid))
        chain.event(task['id'],'independent_review',receipt)
        ledger.add(packet,receipt,packet_path=folder/'packet.json')
        report=receipt['report']
        if report['verdict']=='pass':
            published=publish(tasks,task,stage,result,before,critic={'packet':packet,'receipt':receipt})
            progress('delivered',ledger.contract(task,scope),last_changes=plan['owner_request_response'])
            return published
        archive_result();result=None
        progress('repair',ledger.contract(task,scope),last_changes=plan['owner_request_response'])
        if report['verdict']=='blocked':
            raise ValueError('Revue indépendante : '+report['summary'])
        tasks.update(task['id'],status='producing',message='Reprise ciblée : '+report['summary'][:220])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    config=json.loads(Path(args.config).read_text());repo=Path(config['studio_repository']).resolve()
    allowed_root=Path(config.get('allowed_repository_root', repo.parent)).resolve()
    if not repo.is_relative_to(allowed_root):raise ValueError('Repository must remain inside the explicitly configured workspace root.')
    tasks=StudioTasks(EditorialWorkflow(repo/'output/example/editorial-workflow'))
    tasks.store.root.mkdir(parents=True,exist_ok=True)
    from app.services.studio_delivery import recover_deliveries
    recover_deliveries(tasks.workflow)
    with tasks.store.path('worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Fail closed after interruption, never blindly repeat a possibly completed expensive job.
        for row in tasks.list():
            if row['status'] in {'interpreting','producing','verifying'}:
                pid=row.get('executor_pid')
                if pid:
                    command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
                    if str(Path(config['work_root'])/row['id']) in command and 'codex' in command:
                        os.killpg(pid,signal.SIGTERM)
                tasks.fail(row['id'],'Traitement interrompu : reprise des fichiers conservés.',transient=True)
        while True:
            tasks.store.write('engine.json',{'online_at':now(),'pid':os.getpid()})
            from app.services.editorial_inbox import EditorialInbox
            from app.services.reel_pipeline import ReelPipeline
            for review in EditorialInbox(tasks.workflow,ReelPipeline(tasks.workflow)).list():
                if review['kind']=='run' and review['decision']=='keep':
                    from app.services.reel_covers import ReelCovers
                    ReelCovers(tasks.workflow,ReelPipeline(tasks.workflow)).prepare(review)
                if review['created_at']>=config['activated_at'] or review['id'] in config.get('backlog_review_ids',[]):tasks.enqueue(review)
            task=tasks.claim()
            if task:
                try:execute(tasks,task,config)
                except Exception as e:tasks.fail(task['id'],e,transient=isinstance(e,ConnectionError))
            if args.once:return
            time.sleep(.5)

if __name__=='__main__':main()
