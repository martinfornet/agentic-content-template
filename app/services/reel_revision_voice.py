"""Validate a task-bound, lossless removal from an approved voice as a proposal."""
import copy
import math
import json
import re
import subprocess
import wave
from app.services.reel_pipeline import digest, file_hash
from app.services.revision_contract import scope_for


def removed_beat_candidate(pipeline, project, review):
    proposal=project.get('revision_voice_proposal')
    if not proposal:
        return None
    original=project.get('approved_narration') or {}
    task={'review':review}
    if review.get('kind')!='run' or review.get('decision')!='changes' or scope_for(task)['preserve_voice']:
        raise ValueError('La correction réelle ne permet pas de modifier la voix.')
    if proposal.get('review_id')!=review.get('id') or proposal.get('baseline_run')!=review.get('target_id'):
        raise ValueError('La proposition ne correspond pas au retour propriétaire et au rendu de référence.')
    baseline=next((r for r in project.get('runs',[]) if r.get('id')==review.get('target_id')),None)
    if not baseline:
        raise ValueError('Rendu de référence introuvable.')
    baseline_voice=baseline.get('approved_narration') or baseline.get('inputs',{}).get('approved_narration') or {}
    if baseline_voice.get('sha256')!=original.get('sha256') or baseline_voice.get('music',{}).get('sha256')!=original.get('music',{}).get('sha256'):
        raise ValueError('Voix ou musique de référence différentes de la validation d’origine.')
    if proposal.get('owner_approved_proposal') is not False or proposal.get('original_audio_sha256')!=original.get('sha256'):
        raise ValueError('La proposition doit préserver la validation de la voix initiale.')
    match=re.search(r'(?:enl[èe]ve\w*|supprim\w*|retir\w*)\s+(?:la\s+|le\s+|cette\s+|ce\s+)?(?:partie|passage|mention)\s+(?:sur|de|du|des)\s+(?:la\s+|le\s+|les\s+)?([^.!?;]+)',review.get('note',''),re.I)
    if not match:
        raise ValueError('La suppression ciblée doit être explicite dans le retour.')
    target=match.group(1).strip().casefold()
    draft=copy.deepcopy(project['draft']); beats=draft['beats']
    candidates=[i for i,b in enumerate(beats) if target in b['text'].casefold()]
    if len(candidates)!=1 or beats[candidates[0]]['text']!=proposal.get('affected_text'):
        raise ValueError('Le passage supprimé ne correspond pas de manière unique à la demande.')
    del beats[candidates[0]]
    if len(beats)<4:
        raise ValueError('La suppression laisse trop peu de segments.')
    start,end=proposal.get('cut_seconds', [None,None])
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in (start,end)) or not 0<=start<end:
        raise ValueError('Bornes audio invalides.')
    def safe_path(relative):
        p=pipeline.store.path(relative).resolve()
        if not p.is_relative_to(pipeline.store.root.resolve()) or not p.is_file():
            raise ValueError('Audio absent ou extérieur au projet.')
        return p
    alignment=safe_path((baseline.get('narration') or {}).get('alignment',''))
    timing=json.loads(alignment.read_text())
    bounds=timing.get('boundaries',[])
    index=candidates[0]
    if timing.get('audio_sha256')!=original.get('sha256') or len(bounds)!=len(project['draft']['beats'])+1 or abs(start-bounds[index])>.5 or abs(end-bounds[index+1])>.5:
        raise ValueError('La coupe ne correspond pas à l’alignement du passage demandé.')
    source=safe_path(original['audio_path']); revised=safe_path(proposal['audio_path'])
    if file_hash(source)!=original['sha256'] or file_hash(revised)!=proposal.get('proposed_audio_sha256'):
        raise ValueError('Empreinte audio incorrecte.')
    with wave.open(str(source),'rb') as a, wave.open(str(revised),'rb') as b:
        if a.getparams()[:3]!=b.getparams()[:3] or a.getcomptype()!='NONE':
            raise ValueError('Format PCM incohérent.')
        rate=a.getframerate(); revised_frames=b.getnframes(); first=round(start*rate); last=round(end*rate)
        if last>a.getnframes() or b.getnframes()!=a.getnframes()-(last-first):
            raise ValueError('Durée de coupe incohérente.')
        prefix=a.readframes(first); a.setpos(last)
        if b.readframes(first)!=prefix or b.readframes(b.getnframes()-first)!=a.readframes(a.getnframes()-last):
            raise ValueError('La voix contient des modifications hors de la coupe autorisée.')
    music=proposal.get('music')
    old_music=original.get('music')
    if not isinstance(music,dict) or not isinstance(old_music,dict):
        raise ValueError('Le mix de correction doit conserver la musique choisie.')
    for key in ('id','title','artist','track_key','source_audio_sha256','start_seconds'):
        if music.get(key)!=old_music.get(key):
            raise ValueError('La correction a changé la musique choisie.')
    if music.get('original_mix_sha256')!=old_music.get('sha256') or music.get('voice_sha256')!=proposal['proposed_audio_sha256'] or music.get('story_id')!=original['story_id']:
        raise ValueError('Le mix de correction ne correspond pas à la voix et au mix initiaux.')
    old_mix=safe_path(old_music['audio_path']); revised_mix=safe_path(music['audio_path'])
    if file_hash(old_mix)!=old_music['sha256'] or file_hash(revised_mix)!=music.get('sha256'):
        raise ValueError('Empreinte du mix incorrecte.')
    pcm=subprocess.run([pipeline.config['ffmpeg'],'-v','error','-i',str(old_mix),'-f','s16le','-acodec','pcm_s16le','-ar','48000','-ac','2','-'],check=True,stdout=subprocess.PIPE).stdout
    with wave.open(str(revised_mix),'rb') as f:
        if (f.getnchannels(),f.getsampwidth(),f.getframerate())!=(2,2,48000) or f.getcomptype()!='NONE':
            raise ValueError('Format du mix de correction invalide.')
        actual=f.readframes(f.getnframes())
    a=round(start*48000)*4; b=round(end*48000)*4
    if b>len(pcm) or actual!=pcm[:a]+pcm[b:]:
        raise ValueError('Le mix doit être la coupe exacte du mix choisi.')
    proof=copy.deepcopy(original)
    proof['music']=copy.deepcopy(music)
    proof.update(audio_path=proposal['audio_path'],sha256=proposal['proposed_audio_sha256'],draft_sha256=digest(draft),
                 revision_review_id=review['id'],revision_baseline_run=review['target_id'],proposal=True)
    plan=copy.deepcopy(project.get('visual_plan', []))
    if len(plan)!=len(project['draft']['beats']) or candidates[0]==0:
        raise ValueError('Découpage visuel incompatible avec cette correction.')
    removed=plan.pop(candidates[0]); plan[candidates[0]-1]['shots'].extend(removed.get('shots',[]))
    shots=[shot for slot in plan for shot in slot.get('shots',[])]
    if not shots or any('start_seconds' not in x or 'end_seconds' not in x for x in shots):
        raise ValueError('Découpage visuel explicite requis pour préserver les photos.')
    old_end=shots[-1]['end_seconds']; new_end=round((revised_frames/rate)*30)/30
    if old_end<=0 or new_end<=0:
        raise ValueError('Durée visuelle invalide.')
    for shot in shots:
        shot['start_seconds']=round(shot['start_seconds']/old_end*new_end*30)/30
        shot['end_seconds']=round(shot['end_seconds']/old_end*new_end*30)/30
    return {'draft':draft,'narration':proof,'removed_beat':candidates[0],'visual_plan':plan}
