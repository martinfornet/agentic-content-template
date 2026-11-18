"""Check real photo availability before spending on a new voice. No owner decisions."""
import copy
from app.services.studio_tasks import digest, now


def scope(workflow, place_id):
    place = workflow.read()['places'][place_id]
    return digest({k: place.get(k) for k in ('sources', 'points', 'scope')})


def minimum(workflow, place_id):
    import json
    count = 8
    for path in (workflow.store.root/'reels/projects').glob('*/project.json'):
        project = json.loads(path.read_text())
        if project['place_id'] == place_id:
            count = max([count] + [len(r.get('timeline', [])) for r in project.get('runs', []) if r['status'] == 'rendered'])
    return count


def check(pipeline, place_id, manifest):
    from app.services.visual_quality import audit
    assets = manifest.get('assets', [])
    needed = minimum(pipeline.editorial, place_id)
    if len(assets) < needed or any(a.get('type') not in {'IMAGE', 'VIDEO'} for a in assets):
        raise ValueError(f'Avant la voix : acquérir au moins {needed} plans différents et inspectés (photos ou extraits vidéo).')
    purposes = manifest.get('purposes', [])
    if len(purposes) != len(assets) or any(not isinstance(p, str) or not p.strip() for p in purposes):
        raise ValueError('Chaque photo ou extrait vidéo doit avoir un rôle prévu dans le récit.')
    project = {'place_id': place_id, 'photo_motion': {'enabled': False},
               'visual_reviews': manifest.get('visual_reviews', {})}
    report = audit(pipeline, project, assets=assets, deep=True)
    if not report['passed']:
        raise ValueError('Sourcing avant voix : '+' '.join(report['errors']))
    return dict(report, minimum_photos=needed, minimum_visuals=needed, artistic_approval=False)


def record(pipeline, place_id, manifest):
    report = check(pipeline, place_id, manifest)
    value = {'place_id': place_id, 'scope': scope(pipeline.editorial, place_id),
             'manifest': copy.deepcopy(manifest), 'report': report}
    ident = digest(value)
    path = 'photo-preflights/'+place_id+'/'+ident+'.json'
    old = pipeline.store.read(path)
    if old:
        return old
    value.update(id=ident, created_at=now())
    pipeline.store.write(path, value, immutable=True)
    return value


def require_current(pipeline, place_id):
    import json
    records = sorted([json.loads(p.read_text()) for p in pipeline.store.path('photo-preflights/'+place_id).glob('*.json')],
                     key=lambda r: r['created_at'], reverse=True)
    for record in records:
        if record['scope'] == scope(pipeline.editorial, place_id):
            check(pipeline, place_id, record['manifest'])
            return record
    raise ValueError('Le lot photo doit être sourcé et contrôlé avant cette nouvelle voix.')


def guard(pipeline, place_id):
    context = pipeline.editorial.store.read('production-context.json', {})
    if context.get('photo_preflight_required') and context.get('place_id') == place_id:
        return require_current(pipeline, place_id)
