"""Owner-entered observations: unknown values stay null; no inferred platform metrics."""
import fcntl,math
from datetime import datetime,UTC,timedelta
from app.services.studio_tasks import now
from app.services.reel_publications import ReelPublications

METRICS={'views','reach','saves','shares','follows','average_watch_seconds','retention_percent'}
def record_performance(workflow,pipeline,data):
    run=data.get('run_id');pubs=ReelPublications(workflow,pipeline)
    if not isinstance(run,str) or len(run)>128 or '/' in run:raise ValueError('Reel inconnu.')
    if data.get('checkpoint') not in {'J+1','J+3','J+7'}:raise ValueError('Choisir J+1, J+3 ou J+7.')
    metrics=data.get('metrics',{})
    if not isinstance(metrics,dict) or set(metrics)-METRICS:raise ValueError('Mesure inconnue.')
    for name,value in metrics.items():
        if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0 or name=='retention_percent' and value>100):raise ValueError('Valeur de mesure invalide.')
    try:observed=datetime.fromisoformat(data['observed_at'])
    except (KeyError,TypeError,ValueError):raise ValueError('Date du relevé requise.')
    if observed.tzinfo is None or observed>datetime.now(UTC)+timedelta(minutes=5):raise ValueError('Date avec fuseau horaire requise, sans date future.')
    experiment=validate_experiment(data.get('experiment'))
    if data.get('decision','pending') not in {'pending','keep','adapt','abandon'}:raise ValueError('Décision invalide.')
    for key in ('source','hypothesis','comment','decision'):
        if not isinstance(data.get(key,''),str) or len(data.get(key,''))>5000:raise ValueError('Texte invalide.')
    if not data.get('source','').strip():raise ValueError('Indiquez l’origine du relevé.')
    with (workflow.store.root/'reels/inbox/review.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        row=pubs.store.read(run+'.json')
        if not row or not row.get('published'):raise ValueError('Publication déclarée requise.')
        metadata=row.get('metadata',{})
        if metadata.get('published_at') and observed<datetime.fromisoformat(metadata['published_at']):raise ValueError('Le relevé précède la publication.')
        value={k:data.get(k) for k in ('checkpoint','observed_at','source','hypothesis','comment','decision')}
        value.update(experiment=experiment,metrics={k:metrics.get(k) for k in sorted(METRICS)},recorded_at=now(),actor=workflow.read()['owner_id'],platform_verified=False)
        if row.get('metrics') is None:row['metrics']=[]
        row['metrics'].append(value);pubs.store.write(run+'.json',row)
    return value


def validate_experiment(value):
    if value is None:
        return None
    keys = {'variable', 'variant', 'cohort', 'hypothesis'}
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in value.values()):
        raise ValueError('Expérience : une variable, une variante, un groupe comparable et une hypothèse requis.')
    if value['variable'] not in {'hook', 'ending', 'voice', 'shot_timing', 'image_selection'}:
        raise ValueError('Tester une seule variable éditoriale à la fois.')
    return dict(value)


def performance_view(publication):
    """Derived display only: never rewrite archives or interpret missing metrics as zero."""
    published = publication.get('metadata', {}).get('published_at')
    records = []
    for row in publication.get('metrics') or []:
        metrics = row.get('metrics', {})
        reach = metrics.get('reach')
        rates = {key + '_per_1000_reached': round(metrics[key] / reach * 1000, 2)
                 if isinstance(reach, (int, float)) and reach > 0 and metrics.get(key) is not None else None
                 for key in ('shares', 'saves', 'follows')}
        age = None
        if published:
            age = round((datetime.fromisoformat(row['observed_at']) - datetime.fromisoformat(published)).total_seconds() / 3600, 1)
        records.append(dict(row, rates=rates, age_hours=age))
    return {'records': records, 'checkpoints': [
        {'label': label, 'hours': hours, 'recorded': any(r['checkpoint'] == label for r in records),
         'due_at': (datetime.fromisoformat(published) + timedelta(hours=hours)).isoformat() if published else None}
        for label, hours in [('J+1', 24), ('J+3', 72), ('J+7', 168)]],
        'causality_proven': False, 'notice': 'Comparer à âge, durée, sujet et audience comparables ; aucun seuil universel de viralité.'}
