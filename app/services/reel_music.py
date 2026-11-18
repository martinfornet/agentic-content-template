"""Music choices bound to the exact approved voice, with independent picture timing."""
import copy, math
from app.services.reel_pipeline import digest, file_hash


def choose_music(workflow, story, choice):
    plan=workflow.store.read('music-plans.json',{}).get(story['place_id'],{})
    candidates=plan.get('candidates',[])
    if not candidates and not choice:return None  # Legacy reviews remain readable.
    if not isinstance(choice,dict):raise ValueError('Choisissez la musique A ou B avant de valider.')
    candidate=next((c for c in candidates if c['id']==choice.get('id')),None)
    if not candidate or digest(candidate)!=choice.get('version'):
        raise ValueError('Les propositions musicales ont changé. Rechargez les préécoutes.')
    from app.services.reel_music_usage import unavailable
    blocked=unavailable(workflow,candidate,story['place_id'])
    if blocked:raise ValueError(blocked+' ; choisissez une autre musique.')
    if candidate.get('voice_sha256')!=story['audition']['sha256'] or candidate.get('story_id')!=story['id']:
        raise ValueError('Ces musiques correspondent à une autre prise de voix.')
    if not candidate.get('qa_passed'):raise ValueError('Préécoute musicale non contrôlée.')
    path=workflow.store.path('reels/'+candidate['audio_path']).resolve()
    if not path.is_relative_to((workflow.store.root/'reels').resolve()) or not path.is_file() or file_hash(path)!=candidate['sha256']:
        raise ValueError('Préécoute musicale absente ou modifiée.')
    return copy.deepcopy(candidate)


from creator.timing import snap_music_cuts


def selected_mix(pipeline, proof, duration):
    music=proof.get('music')
    if not music:return None
    if music['voice_sha256']!=proof['sha256'] or music['story_id']!=proof['story_id'] or not music.get('qa_passed'):
        raise ValueError('Musique validée liée à une autre voix.')
    path=pipeline.store.path(music['audio_path']).resolve()
    if not path.is_relative_to(pipeline.store.root.resolve()) or not path.is_file() or file_hash(path)!=music['sha256']:
        raise ValueError('Mix validé absent ou modifié.')
    if abs(float(pipeline.probe(path)['format']['duration'])-duration)>.1:raise ValueError('Durée du mix incompatible avec la voix.')
    return path
