"""An explicit owner correction may reuse one exact opening/closing photograph.

The authority is the real immutable inbox review, never a producer flag or a
global quality setting. The grant is limited to its project, baseline and task.
All other photo checks still apply.
"""
import re


def prepare_authorization(pipeline, target, request):
    from app.services.reel_pipeline import file_hash
    if not isinstance(request, dict) or set(request) != {'asset_sha256', 'shots'}:
        raise ValueError('Exception photo : empreinte et positions explicites requises.')
    sha, shots = request['asset_sha256'], request['shots']
    timeline = target.get('timeline', [])
    if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha):
        raise ValueError('Empreinte de la photo invalide.')
    if (not isinstance(shots, list) or any(type(i) is not int for i in shots)
            or len(timeline) < 2 or shots != [1, len(timeline)]):
        raise ValueError('Exception limitée aux plans d’ouverture et de fin du rendu exact.')
    candidates = {a['file_path']: a for a in target.get('inputs', {}).get('assets', []) if a}
    source = candidates.get(timeline[-1].get('asset'))
    if not source or source['type'] != 'IMAGE' or source.get('metadata', {}).get('sha256') != sha:
        raise ValueError('La photo autorisée doit être celle du dernier plan de référence.')
    if file_hash(pipeline.store.path(source['file_path'])) != sha:
        raise ValueError('La photo de référence a été modifiée.')
    return {'project_id': target['project_id'], 'place_id': target['place_id'],
            'base_run_id': target['id'], 'base_video_sha256': target['sha256'],
            'asset_sha256': sha, 'shots': shots, 'shot_count': len(timeline)}


def authorization(pipeline, project, *, require_latest=False):
    ident = project.get('photo_reuse_review_id')
    if ident is None:
        return None
    if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise ValueError('Référence d’exception photo invalide.')
    review = pipeline.store.read('inbox/' + ident + '.json', {})
    grant = review.get('photo_reuse')
    if (not isinstance(grant, dict) or not grant or review.get('id') != ident or review.get('kind') != 'run'
            or review.get('decision') != 'changes'
            or review.get('actor') != pipeline.editorial.read()['owner_id']
            or review.get('place_id') != project.get('place_id')
            or grant.get('project_id') != project.get('id')
            or grant.get('place_id') != project.get('place_id')
            or grant.get('base_run_id') != review.get('target_id')):
        raise ValueError('Exception photo sans autorisation propriétaire pour ce projet.')
    base = next((r for r in project.get('runs', []) if r['id'] == grant['base_run_id']), None)
    if not base or base.get('sha256') != grant.get('base_video_sha256'):
        raise ValueError('La référence de l’exception photo a changé.')
    expected = prepare_authorization(pipeline, dict(base, project_id=project['id'], place_id=project['place_id']),
                                     {k:grant.get(k) for k in ('asset_sha256','shots')})
    if grant != expected:
        raise ValueError('Périmètre de l’exception photo incohérent.')
    if require_latest:
        from app.services.editorial_inbox import EditorialInbox
        reviews = [r for r in EditorialInbox(pipeline.editorial, pipeline).list() if r['place_id'] == project['place_id']]
        if not reviews or reviews[-1]['id'] != ident:
            raise ValueError('Cette exception photo appartient à une autre demande ; nouvel accord explicite requis.')
    return dict(grant, review_id=ident)


def permits(grant, previous, current, previous_shot, current_shot, shot_count):
    return bool(grant and grant['shot_count'] == shot_count
                and grant['shots'] == [previous_shot, current_shot]
                and previous.get('sha256') == current.get('sha256') == grant['asset_sha256'])


def require_task_binding(task, run):
    ident = run.get('inputs', {}).get('photo_reuse_review_id')
    if ident is not None and (ident != task['review']['id'] or task['review'].get('kind') != 'run'
                              or not task['review'].get('photo_reuse')):
        raise ValueError('L’exception photo est réservée à sa correction propriétaire exacte.')
