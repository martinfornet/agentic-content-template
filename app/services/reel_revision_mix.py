"""A music-only correction bound to the exact previously rendered Reel."""
import copy
from app.services.reel_pipeline import digest, file_hash
from app.services.revision_contract import scope_for


def mix_candidate(pipeline, project, review):
    proposal=project.get('revision_mix_proposal')
    if not isinstance(proposal,dict):return None
    scope=scope_for({'review':review})
    if review.get('kind')!='run' or review.get('decision')!='changes' or not scope['preserve_voice'] or scope['preserve_music']:
        raise ValueError('La demande ne correspond pas à une correction de musique seule.')
    if proposal.get('review_id')!=review.get('id') or proposal.get('baseline_run')!=review.get('target_id') or proposal.get('owner_approved_proposal') is not False:
        raise ValueError('Mix de correction non lié au retour et au rendu exacts.')
    baseline=next((r for r in project.get('runs',[]) if r.get('id')==review.get('target_id') and r.get('status')=='rendered'),None)
    if not baseline:raise ValueError('Rendu de référence introuvable.')
    inputs=baseline.get('inputs') or {}
    from app.services.reel_editing import visual_assets
    if visual_assets(project)!=inputs.get('assets'):
        raise ValueError('Une correction musicale doit conserver les images du rendu de référence.')
    voice=baseline.get('approved_narration') or inputs.get('approved_narration') or {}
    if not voice.get('sha256') or voice.get('draft_sha256')!=digest(inputs.get('draft')):
        raise ValueError('Voix ou texte du rendu de référence incohérents.')
    def verified(path,sha):
        p=pipeline.store.path(path).resolve()
        if not p.is_relative_to(pipeline.store.root.resolve()) or not p.is_file() or file_hash(p)!=sha:
            raise ValueError('Fichier de correction absent ou modifié.')
        return p
    verified(voice['audio_path'],voice['sha256'])
    old=voice.get('music') or {};music=proposal.get('music') or {}
    for k in ('id','title','artist','track_key','source_audio_sha256','start_seconds'):
        if music.get(k)!=old.get(k):raise ValueError('La correction musicale doit conserver le morceau choisi.')
    if music.get('voice_sha256')!=voice['sha256'] or music.get('story_id')!=voice.get('story_id') or music.get('original_mix_sha256')!=old.get('sha256') or not music.get('qa_passed'):
        raise ValueError('Le nouveau mix ne correspond pas à la voix et au mix de référence.')
    prefix=pipeline.path(project['id'],'revision-proposals/'+review['id']+'/')
    if not str(music.get('audio_path','')).startswith(prefix):raise ValueError('Le mix doit être enregistré dans la proposition de cette correction.')
    verified(music['audio_path'],music['sha256'])
    newvoice=copy.deepcopy(voice);newvoice['music']=copy.deepcopy(music)
    newvoice.update(mix_revision_review_id=review['id'],mix_revision_baseline_run=review['target_id'])
    return {'draft':copy.deepcopy(inputs['draft']),'narration':newvoice,'visual_plan':copy.deepcopy(inputs.get('visual_plan',[]))}
