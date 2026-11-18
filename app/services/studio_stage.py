"""Copy code, metadata and place inputs only. Never hard-link writable production data."""
import json
from pathlib import Path
import shutil


def prepare_stage(repository,stage,before):
    repository,stage=Path(repository).resolve(),Path(stage)
    stage.mkdir(parents=True)
    copied=set()
    def copy(path):
        path=Path(path).resolve()
        if not path.is_relative_to(repository) or not path.is_file():return
        rel=path.relative_to(repository)
        if rel in copied:return
        # Root models are private weights; app/models contains required Python types.
        if rel.parts[0]=='models' or any(x in rel.parts for x in ('.git','__pycache__','immediate-tasks','deliveries','published')):return
        if path.name.endswith(('.lock','.tmp','.pyc')) or path.name.startswith('.env'):return
        target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target);copied.add(rel)
    for name in ('app','brands'):
        for path in (repository/name).rglob('*'):copy(path)
    for name in ('studio_recipe.py','studio_backup.py','studio_references.py'):copy(repository/'tools'/name)
    copy(repository/'docs/BRAND_DIRECTION.md')
    copy(repository/'docs/GENERATION_QUALITY.md')
    copy(repository/'docs/PRODUCTION_CHAIN.md')
    workflow=repository/'output/example/editorial-workflow'
    # Preserve all small immutable records and actual owner reviews for memory/fencing.
    for path in (repository/'output').rglob('*.json'):
        copy(path)
    def collect(value):
        if isinstance(value,dict):
            for v in value.values():collect(v)
        elif isinstance(value,list):
            for v in value:collect(v)
        elif isinstance(value,str) and len(value)<1024 and '\n' not in value and not value.startswith(('http:','https:')):
            for root in (repository,workflow,workflow/'reels'):
                try:copy(root/value)
                except (OSError,ValueError):pass
    collect(before)
    from app.services.editorial_workflow import EditorialWorkflow
    from app.services.reel_pipeline import ReelPipeline
    from app.services.generation_quality import approved_voice_references
    w=EditorialWorkflow(workflow)
    if w.store.path('workflow.json').is_file():
        p=ReelPipeline(w)
        for reference in approved_voice_references(w,p):
            copy(workflow/'reels'/reference['audio_path'])
    # Keep all versions of this place's story artifacts, but no unrelated media.
    place=before.get('place_id') or (before['story']['place_id'] if before.get('story') else before['reviews'][-1]['place_id'])
    for record in (workflow/'reels/photo-preflights'/place).glob('*.json'):
        collect(json.loads(record.read_text()))
    for record in (workflow/'reels/story-proposals').glob('*.json'):
        if json.loads(record.read_text()).get('place_id')==place:
            for path in record.with_suffix('').rglob('*'):copy(path)
    return {'files':len(copied),'bytes':sum((stage/r).stat().st_size for r in copied)}
