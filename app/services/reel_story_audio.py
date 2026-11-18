"""Listen to a sourced story before choosing a place for publication, offline."""
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess


def _candidate(pipeline, place_id, *, story, candidate_index):
    from app.services.reel_story import StoryPreparation
    from app.services.reel_pipeline import dependency_hash, digest, file_hash
    from app.services.reel_alignment import prepare
    from app.services.reel_audio import measure_audio
    story=story or StoryPreparation(pipeline.editorial).latest(place_id)
    if not story or story.get('stale',False):
        raise ValueError('Récit sourcé actuel requis avant audition.')
    from app.services.photo_preflight import guard
    guard(pipeline, place_id)
    preflight=pipeline.preflight()
    if not preflight['ready']:
        raise ValueError('Voix locale indisponible : '+', '.join(preflight['missing']))
    dependencies={name:dependency_hash(pipeline.repository/'app/services'/name) for name in ('reel_narration.py','reel_alignment.py','reel_audio.py')}
    dependencies['pronunciation']=dependency_hash(pipeline.repository/'brands/example/pronunciation.json')
    dependencies['reference']=dependency_hash(pipeline.config_path('voice_reference'))
    dependencies['model']=dependency_hash(pipeline.config_path('voice_checkpoint')/'t3_mtl23ls_v3.safetensors')
    key=digest({'story':story['id'],'voice':pipeline.config['narration'],'dependencies':dependencies,'candidate_index':candidate_index})
    relative=f"story-proposals/{story['id']}/candidates/{key[:20]}"
    folder=pipeline.store.path(relative+'/audition.json').parent;folder.mkdir(parents=True,exist_ok=True)
    receipt=pipeline.store.read(relative+'/audition.json')
    output=folder/'voice.wav'
    if receipt and output.exists() and receipt.get('sha256')==file_hash(output):return receipt
    os.environ.setdefault('NUMBA_CACHE_DIR',str(pipeline.store.path('numba-cache')))
    error=None
    try:
        audio,_=prepare(pipeline,{'draft':story['draft'],'voice_variant':candidate_index},folder,preflight['paths'],folder/'generation.log')
    except ValueError as failure:
        # Keep a generated take available to listen to when ASR disagrees.
        audio=folder/'narration.wav'
        if not audio.exists():raise
        error=str(failure)
    temporary=folder/'voice.tmp.wav'
    pipeline.run_command([pipeline.config['ffmpeg'],'-v','error','-y','-i',str(audio),'-af',
                          'loudnorm=I=-16:TP=-1.5:LRA=11','-ar','48000','-ac','2','-c:a','pcm_s16le',str(temporary)],folder/'generation.log')
    temporary.replace(output)
    qa=measure_audio(output,ffmpeg=pipeline.config['ffmpeg'])
    receipt={'story_id':story['id'],'place_id':place_id,'mode':'audio_audition',
             'status':'ready_to_listen' if not error and qa['passed'] else 'needs_review',
             'audio_path':relative+'/voice.wav','sha256':file_hash(output),
             'duration':float(pipeline.probe(output)['format']['duration']),
             'alignment_passed':not error,'alignment_error':error,'technical_audio':qa,
             'created_at':datetime.now(UTC).isoformat(),'listening_approved':False,
             'publication_choice_created':False,'approval_created':False,'api_cost_usd':0,'candidate_only':True,'candidate_index':candidate_index}
    pipeline.store.write(relative+'/audition.json',receipt)
    return receipt


def audition(pipeline, place_id, *, story=None, count=2):
    """Bounded, resumable audition batch. No candidate becomes the visible take implicitly."""
    from app.services.reel_story import StoryPreparation
    from app.services.generation_quality import approved_voice_references, current_voice_approved
    if type(count) is not int or not 2 <= count <= 3:
        raise ValueError('Deux ou trois prises maximum par comparaison.')
    story = story or StoryPreparation(pipeline.editorial).latest(place_id)
    if not story or story.get('stale'):
        raise ValueError('Récit actuel requis.')
    # A bulk preparation must never regenerate an accepted take.
    accepted = approved_voice_references(pipeline.editorial, pipeline)
    existing = story.get('audition', {})
    if current_voice_approved(pipeline.editorial, pipeline, story):
        return existing
    from app.services.photo_preflight import guard
    guard(pipeline, place_id)
    from app.services.revision_progress import require_plan
    require_plan(pipeline.editorial)
    candidates = [_candidate(pipeline, place_id, story=story, candidate_index=i) for i in range(count)]
    return {'story_id': story['id'], 'status': 'selection_required', 'candidates': candidates,
            'approved_references': accepted, 'approval_created': False, 'api_cost_usd': 0}


def select_audition(pipeline, story, candidate, report):
    """Persist a new selected proposal, with a content-addressed decision and no approval."""
    from app.services.generation_quality import validate_voice_selection, approved_voice_references, current_voice_approved
    from app.services.reel_pipeline import digest, file_hash
    from app.services.reel_story import StoryPreparation
    current = StoryPreparation(pipeline.editorial).latest(story['place_id'])
    if not current or current['id'] != story['id'] or current.get('stale'):
        raise ValueError('Le récit a changé pendant la comparaison.')
    if current_voice_approved(pipeline.editorial, pipeline, current):
        raise ValueError('La prise courante est validée : conserver cet accord et passer par une demande de correction.')
    from app.services.photo_preflight import guard
    guard(pipeline, story['place_id'])
    if candidate.get('story_id') != story['id'] or candidate.get('status') != 'ready_to_listen' or not candidate.get('alignment_passed') or not candidate.get('technical_audio', {}).get('passed'):
        raise ValueError('La prise retenue doit passer les contrôles de voix et d’alignement.')
    if file_hash(pipeline.store.path(candidate['audio_path'])) != candidate['sha256']:
        raise ValueError('Prise modifiée depuis son contrôle.')
    validate_voice_selection(pipeline, story, candidate, report)
    key = digest(report)
    relative = 'story-proposals/' + story['id'] + '/auditions/selected-' + key[:20]
    old = pipeline.store.read(relative + '/audition.json')
    if old:
        return old
    pipeline.store.write(relative + '/selection.json', report, immutable=True)
    selected = dict(candidate, candidate_only=False, selection_path=relative + '/selection.json',
                    selection_sha256=key, created_at=datetime.now(UTC).isoformat(),
                    listening_approved=False, approval_created=False)
    pipeline.store.write(relative + '/audition.json', selected, immutable=True)
    return selected
