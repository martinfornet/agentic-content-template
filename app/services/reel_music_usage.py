"""Project-wide music exclusivity: auditions are proposals, delivered Reels consume tracks."""
import json,re,unicodedata

def track_key(music):
    title=re.sub(r'\([^)]*\)|\[[^]]*\]','',music.get('title',''))
    def clean(value):
        return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode().lower())
    artist=clean(music.get('artist',''))
    aliases={'thedavebrubeckquartet':'davebrubeck','davebrubeckquartet':'davebrubeck'}
    return clean(title)+':'+aliases.get(artist,artist)

def usage(workflow):
    """Read actual delivered projects plus durable receipts; no decision is fabricated."""
    records=list(workflow.store.read('music-usage.json',{}).get('used',[]))
    root=workflow.store.root/'reels'
    for path in root.glob('projects/*/project.json'):
        project=json.loads(path.read_text())
        for run in project.get('runs',[]):
            if run.get('status')!='rendered':continue
            music=run.get('music') or (run.get('approved_narration') or run.get('inputs',{}).get('approved_narration') or {}).get('music')
            if not music or not music.get('title'):continue
            if not any(r.get('run_id')==run['id'] for r in records):
                records.append(dict(key=track_key(music),title=music['title'],artist=music.get('artist',''),place_id=project['place_id'],run_id=run['id'],reason='integrated_in_delivered_reel'))
    return records

def reservations(workflow):
    """Reserve a real current choice while its montage is being prepared."""
    from app.services.reel_story import StoryPreparation
    latest={}
    for path in (workflow.store.root/'reels/inbox').glob('*.json'):
        r=json.loads(path.read_text());old=latest.get(r['place_id'])
        if old is None or r['created_at']>old['created_at']:latest[r['place_id']]=r
    result=[]
    for place,r in latest.items():
        if r['kind']!='story' or r['decision']!='keep' or not r.get('music'):continue
        story=StoryPreparation(workflow).latest(place)
        if story and story['id']==r['target_id'] and story.get('audition',{}).get('sha256')==r.get('audition_sha256'):
            result.append(dict(key=track_key(r['music']),place_id=place,review_id=r['id']))
    return result

def unavailable(workflow,music,place_id,*,existing_reel=False,used=None,reserved=None):
    key=track_key(music)
    used=next((r for r in (usage(workflow) if used is None else used) if r['key']==key and (not existing_reel or r['place_id']!=place_id)),None)
    if used:return 'Déjà utilisée dans un Reel livré : '+used['place_id']
    reserved=next((r for r in (reservations(workflow) if reserved is None else reserved) if r['key']==key and r['place_id']!=place_id),None)
    if reserved:return 'Déjà choisie pour un autre Reel en préparation : '+reserved['place_id']
    return None

def record_delivered(workflow,music,place_id,run_id):
    rows=usage(workflow)
    if not any(r.get('run_id')==run_id for r in rows):
        rows.append(dict(key=track_key(music),title=music['title'],artist=music.get('artist',''),place_id=place_id,run_id=run_id,reason='integrated_in_delivered_reel'))
    workflow.store.write('music-usage.json',{'policy':'no_reproposal_after_delivered_reel','used':rows})
