"""A new unapproved narration for one actual requested Reel correction."""
import copy
from app.services.reel_pipeline import digest, file_hash
from app.services.revision_contract import scope_for
from app.services.reel_story import StoryPreparation, place_packet, review_story
from app.services.generation_quality import selection_report


def story_candidate(pipeline, project, review):
    proposal=project.get('revision_story_proposal')
    if not isinstance(proposal,dict):
        return None
    if review.get('kind')!='run' or review.get('decision')!='changes' or scope_for({'review':review})['preserve_voice']:
        raise ValueError('La correction réelle ne permet pas une nouvelle narration.')
    if proposal.get('review_id')!=review.get('id') or proposal.get('baseline_run')!=review.get('target_id') or proposal.get('owner_approved_proposal') is not False:
        raise ValueError('Proposition de récit non liée au retour réel.')
    original=project.get('approved_narration') or {}
    baseline=next((r for r in project.get('runs',[]) if r.get('id')==review.get('target_id')),None)
    previous=(baseline or {}).get('approved_narration') or (baseline or {}).get('inputs',{}).get('approved_narration') or {}
    if not baseline or not previous.get('sha256') or not previous.get('story_id'):
        raise ValueError('Voix du rendu de référence introuvable.')
    for key in ('id','title','artist','track_key','source_audio_sha256','start_seconds'):
        if previous.get('music',{}).get(key)!=original.get('music',{}).get(key):
            raise ValueError('La musique du rendu de référence diffère du choix initial.')
    story=StoryPreparation(pipeline.editorial).latest(project['place_id'])
    if not story or story.get('stale') or story.get('author')!='assistant' or story.get('id')!=proposal.get('story_id') or story.get('id')==original.get('story_id'):
        raise ValueError('Nouvelle proposition de récit courante requise.')
    if review_story(place_packet(pipeline.editorial,project['place_id']),story['draft'])['reference_errors']:
        raise ValueError('Références du récit corrigé incohérentes.')
    audition=story.get('audition',{})
    if audition.get('status')!='ready_to_listen' or not audition.get('alignment_passed'):
        raise ValueError('Voix corrigée et alignement requis.')
    path=pipeline.store.path(audition['audio_path']).resolve()
    if not path.is_relative_to(pipeline.store.root.resolve()) or not path.is_file() or file_hash(path)!=audition.get('sha256'):
        raise ValueError('Voix corrigée absente ou modifiée.')
    if audition.get('sha256')==previous.get('sha256'):
        raise ValueError('La correction demandée doit modifier la prise de voix.')
    selection_report(pipeline,story)
    old_music=original.get('music') or {};music=proposal.get('music') or {}
    for key in ('id','title','artist','track_key','source_audio_sha256','start_seconds'):
        if not old_music.get(key) and key not in ('start_seconds',):
            raise ValueError('Identité de la musique initiale incomplète.')
        if music.get(key)!=old_music.get(key):
            raise ValueError('La musique choisie ne doit pas changer.')
    if music.get('voice_sha256')!=audition.get('sha256') or music.get('story_id')!=story['id'] or not music.get('qa_passed'):
        raise ValueError('Mix musical non lié à la nouvelle voix.')
    mixed=pipeline.store.path(music.get('audio_path','')).resolve()
    if not mixed.is_relative_to(pipeline.store.root.resolve()) or not mixed.is_file() or file_hash(mixed)!=music.get('sha256'):
        raise ValueError('Mix musical absent ou modifié.')
    if len(story['draft']['beats'])!=len(project.get('visual_plan',[])):
        raise ValueError('Adapter le découpage au nombre de segments du récit corrigé.')
    proof={'review_id':original['review_id'],'story_id':story['id'],'audio_path':audition['audio_path'],
           'sha256':audition['sha256'],'draft_sha256':digest(story['draft']),'music':copy.deepcopy(music),
           'revision_review_id':review['id'],'revision_baseline_run':review['target_id'],'proposal':True}
    return {'draft':copy.deepcopy(story['draft']),'narration':proof,'visual_plan':copy.deepcopy(project['visual_plan'])}
