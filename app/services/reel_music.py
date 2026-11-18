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


def snap_music_cuts(shots, pulses, max_shift=.5, min_hold=.65):
    if not shots:return []
    if not isinstance(pulses,list) or any(isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t) or t<0 for t in pulses):
        raise ValueError('Pulsations musicales invalides.')
    result=copy.deepcopy(shots);cuts=[shots[0]['start']]
    for i,shot in enumerate(shots[1:],1):
        target=shot['start']; upper=shots[i]['end']-min_hold
        candidates=[round(t*30)/30 for t in pulses if abs(t-target)<=max_shift and round(t*30)/30>=cuts[-1]+min_hold and round(t*30)/30<=upper]
        cuts.append(min(candidates,key=lambda t:abs(t-target)) if candidates else target)
    cuts.append(shots[-1]['end'])
    if any(b-a<.3-1e-6 for a,b in zip(cuts,cuts[1:])):raise ValueError('Calage musical trop serré.')
    for i,s in enumerate(result):s.update(start=cuts[i],end=cuts[i+1])
    return result


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
