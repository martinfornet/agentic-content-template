"""Voice-led caption groups and restrained motion for real still photographs."""
import math
import re


def explicit_caption_timeline(items, beats, duration=None):
    """Accept only chronological caption groups containing the exact narration."""
    if not isinstance(items, list) or not items:
        raise ValueError('Chronologie de sous-titres vide.')
    prior = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
            raise ValueError('Carton de sous-titres invalide.')
        start, end = item.get('start'), item.get('end')
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end)) or start < prior or end <= start:
            raise ValueError('Horodatage des sous-titres invalide.')
        if duration is not None and end > duration + .05:
            raise ValueError('Sous-titres au-delà de la voix.')
        prior = end
    if ' '.join(' '.join(i['text'].split()) for i in items) != ' '.join(' '.join(b['text'].split()) for b in beats):
        raise ValueError('Les sous-titres doivent conserver exactement les mots et la ponctuation du récit.')
    return [dict(start=i['start'], end=i['end'], text=i['text']) for i in items]


from creator.timing import subtitle_chunks


def photo_filter(seconds, policy=None):
    base = 'scale=1080:1920:force_original_aspect_ratio=increase:out_range=tv:out_color_matrix=bt709,crop=1080:1920,setsar=1'
    if not policy or not policy.get('enabled'):
        return base + ',fps=30'
    zoom = policy.get('maximum_zoom', 1.035)
    if not math.isfinite(zoom) or not 1 <= zoom <= 1.08:
        raise ValueError('Mouvement photo hors limites.')
    frames = max(1, round(seconds * 30))
    return (base + f",zoompan=z='1+{zoom-1:.6f}*on/{max(1,frames-1)}':"
            f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={frames}:s=1080x1920:fps=30")


def overlay_filter(items, start, end):
    """A separate timed overlay chain leaves picture cuts independent of words."""
    visible = [item for item in items if item['start'] < end and item['end'] > start]
    chain, prior = [], 'v'
    for index, item in enumerate(visible):
        low, high = max(0, item['start'] - start), min(end - start, item['end'] - start)
        target = f'caption{index}'
        chain.append(f"[{prior}][{index+1}:v]overlay=0:0:format=auto:enable='gte(t,{low:.6f})*lt(t,{high:.6f})'[{target}]")
        prior = target
    chain.append(f'[{prior}]format=yuv420p[out]')
    return visible, ';'.join(chain)


def shot_sequence(plan, beat, asset_count):
    sequence=plan.get('shots') or [{'asset_beat':beat,'weight':1}]
    if not isinstance(sequence,list) or not 1<=len(sequence)<=4:
        raise ValueError('Un à quatre plans visuels requis par passage.')
    for shot in sequence:
        if not isinstance(shot,dict):raise ValueError('Plan visuel invalide.')
        index,weight=shot.get('asset_index',shot.get('asset_beat')),shot.get('weight',1)
        if 'asset_index' in shot and 'asset_beat' in shot and shot['asset_index']!=shot['asset_beat']:
            raise ValueError('Deux références contradictoires pour le même plan.')
        if type(index) is not int or not 0<=index<asset_count:
            raise ValueError('Référence de visuel inconnue dans le découpage.')
        if isinstance(weight,bool) or not isinstance(weight,(int,float)) or not math.isfinite(weight) or not .1<=weight<=10:
            raise ValueError('Répartition de durée visuelle invalide.')
        if 'start_seconds' in shot or 'end_seconds' in shot:
            start,end=shot.get('start_seconds'),shot.get('end_seconds')
            if not all(type(t) in (int,float) and math.isfinite(t) for t in (start,end)) or start<0 or end<=start:
                raise ValueError('Bornes explicites du plan invalides.')
    return sequence


def has_explicit_picture_times(plan):
    return any('start_seconds' in shot or 'end_seconds' in shot
               for slot in plan or [] for shot in slot.get('shots',[]))


def visual_assets(project):
    """A visual pool independent of spoken paragraphs, including unsourced slots."""
    indices=list(range(len(project['draft']['beats'])))
    for key in project.get('assets',{}):
        if not str(key).isdigit() or not 0<=int(key)<48:
            raise ValueError('Index de photo invalide (0 à 47).')
        indices.append(int(key))
    for slot in project.get('visual_plan',[]):
        for shot in slot.get('shots',[]):
            index=shot.get('asset_index',shot.get('asset_beat'))
            if type(index) is not int or not 0<=index<48:
                raise ValueError('Référence de photo invalide (0 à 47).')
            indices.append(index)
    return [project.get('assets',{}).get(str(i)) for i in range(max(indices,default=-1)+1)]


def picture_timeline(beats, assets, boundaries, plan=None):
    """Frame-exact picture cuts; speech and subtitle tracks stay continuous."""
    if len(boundaries)!=len(beats)+1 or len(assets)<len(beats):
        raise ValueError('Chronologie visuelle incohérente.')
    if any(not math.isfinite(t) or t<0 for t in boundaries) or boundaries[0]!=0:
        raise ValueError('Bornes visuelles invalides.')
    plan=plan or [{} for _ in beats]
    if len(plan)!=len(beats):raise ValueError('Découpage à réviser après modification du récit.')
    if any(b<=a for a,b in zip(boundaries,boundaries[1:])):
        raise ValueError('Bornes visuelles non croissantes.')
    if has_explicit_picture_times(plan):
        # Storyboard slots retain narrative intent; their cuts can cross speech
        # paragraphs. Audio and word-aligned captions keep their own timeline.
        timeline=[];cursor=0;last=round(boundaries[-1]*30)
        for beat,slot in enumerate(plan):
            for shot in shot_sequence(slot,beat,len(assets)):
                if 'start_seconds' not in shot or 'end_seconds' not in shot:
                    raise ValueError('Tous les plans doivent avoir des bornes explicites.')
                start,end=(round(shot[key]*30) for key in ('start_seconds','end_seconds'))
                if start!=cursor or end>last or end-start<9:
                    raise ValueError('Coupes explicites discontinues, hors durée ou trop courtes.')
                timeline.append({'beat_index':beat,'asset_index':shot.get('asset_index',shot.get('asset_beat')),
                                 'start':start/30,'end':end/30})
                cursor=end
        if cursor!=last:
            raise ValueError('Les coupes explicites doivent couvrir toute la voix.')
        return timeline
    timeline=[]
    for beat in range(len(beats)):
        sequence=shot_sequence(plan[beat],beat,len(assets))
        start,end=round(boundaries[beat]*30),round(boundaries[beat+1]*30)
        total=sum(s.get('weight',1) for s in sequence);weight=0;cursor=start
        for shot in sequence:
            weight+=shot.get('weight',1)
            target=start+round((end-start)*weight/total)
            if target-cursor<9:raise ValueError('Plan trop court : conserver au moins 0,3 seconde par image.')
            timeline.append({'beat_index':beat,'asset_index':shot.get('asset_index',shot.get('asset_beat')),'start':cursor/30,'end':target/30})
            cursor=target
    return timeline
