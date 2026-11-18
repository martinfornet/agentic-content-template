"""Evidence-bound generation decisions. Technical checks never certify artistic taste."""
import copy
import hashlib
import json
import math
from pathlib import Path
from app.services.studio_tasks import digest, now

VOICE_DIMENSIONS = ('naturalness', 'continuity', 'expression', 'pronunciation', 'ending')
MONTAGE_DIMENSIONS = ('opening', 'progression', 'timing', 'image_words', 'subtitles', 'mix', 'ending')


def text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 5000:
        raise ValueError(label + ' : constat précis requis.')
    return value


def exact_file(store, row):
    path = store.path(row['file_path'])
    if path.is_symlink() or not path.resolve().is_relative_to(store.root.resolve()) or not path.is_file():
        raise ValueError('Média de comparaison absent ou hors dossier.')
    if hashlib.sha256(path.read_bytes()).hexdigest() != row.get('sha256'):
        raise ValueError('Média de comparaison modifié.')
    return path


def validate_direction(packet, draft):
    """Check all three alternatives against the same factual packet, without scoring them."""
    from app.services.reel_story import review_story
    value = draft.get('generation')
    if not isinstance(value, dict):
        raise ValueError('Préparation requise : trois accroches, image initiale et chute associées.')
    hooks = value.get('hooks', [])
    selected = value.get('selected_hook')
    if len(hooks) != 3 or type(selected) is not int or not 0 <= selected < 3:
        raise ValueError('Comparer trois accroches et sélectionner leur index (0 à 2).')
    if len({h.get('text') for h in hooks}) != 3:
        raise ValueError('Les trois accroches doivent être distinctes.')
    for hook in hooks:
        probe = copy.deepcopy(draft)
        probe['beats'][0] = hook
        errors = review_story(packet, probe)['reference_errors']
        if errors:
            raise ValueError('Accroche sans preuve cohérente : ' + '; '.join(errors))
        for key in ('first_image', 'payoff', 'reason'):
            text(hook.get(key), 'Accroche / ' + key)
    if hooks[selected]['text'] != draft['beats'][0]['text']:
        raise ValueError('L’accroche retenue diffère du texte produit.')
    text(value.get('share_reason'), 'Raison de partager')
    text(value.get('selection_reason'), 'Choix de l’accroche')
    validate_voice_direction(value.get('voice_direction'), draft)
    return value


def validate_voice_direction(rows, draft):
    if not isinstance(rows, list) or len(rows) != len(draft['beats']):
        raise ValueError('Une intention et une respiration documentées par passage sont requises.')
    for index, row in enumerate(rows):
        if row.get('beat') != index + 1:
            raise ValueError('Ordre des intentions vocales incohérent.')
        text(row.get('intention'), 'Intention vocale')
        text(row.get('breathing'), 'Respiration utile')
        emphasis = row.get('emphasis', [])
        if not isinstance(emphasis, list) or not emphasis or any(not isinstance(word, str) or not word.strip() or word.casefold() not in draft['beats'][index]['text'].casefold() for word in emphasis):
            raise ValueError('Les mots à appuyer doivent appartenir au passage.')


def approved_voice_references(workflow, pipeline):
    """Only genuine, exact owner-approved takes; never promote generated candidates."""
    from app.services.editorial_inbox import EditorialInbox
    result = []
    for review in EditorialInbox(workflow, pipeline).list():
        if review['kind'] != 'story' or review['decision'] != 'keep' or review.get('actor') != workflow.read()['owner_id']:
            continue
        root = pipeline.store.path('story-proposals/' + review['target_id'])
        story = pipeline.store.read('story-proposals/' + review['target_id'] + '.json', {})
        for path in (root / 'auditions').glob('*/audition.json'):
            take = json.loads(path.read_text())
            if take.get('sha256') != review.get('audition_sha256'):
                continue
            try:
                exact_file(pipeline.store, {'file_path': take['audio_path'], 'sha256': take['sha256']})
            except (ValueError, OSError, KeyError):
                continue
            result.append({'review_id': review['id'], 'story_id': review['target_id'],
                           'place_id': review['place_id'], 'sha256': take['sha256'],
                           'audio_path': take['audio_path'], 'approved_at': review['created_at'],
                           'passages': [b['text'] for b in story.get('draft', {}).get('beats', [])]})
            break
    return result


def validate_voice_selection(pipeline, story, audition, report):
    if report.get('story_id') != story['id'] or report.get('selected_sha256') != audition.get('sha256'):
        raise ValueError('Comparaison vocale liée à une autre prise.')
    validate_voice_direction(report.get('voice_direction'), story['draft'])
    candidates = report.get('candidates', [])
    if not 2 <= len(candidates) <= 3 or len({r.get('sha256') for r in candidates}) != len(candidates):
        raise ValueError('Deux ou trois prises vocales réellement différentes sont requises.')
    if audition['sha256'] not in {r['sha256'] for r in candidates}:
        raise ValueError('La voix retenue ne figure pas dans la comparaison.')
    for row in candidates:
        exact_file(pipeline.store, row)
        if row.get('review_method') not in {'audio_listening', 'signal_and_transcript'}:
            raise ValueError('Méthode de comparaison vocale à préciser.')
        for key in VOICE_DIMENSIONS:
            text(row.get('observations', {}).get(key), 'Voix / ' + key)
        if row['review_method'] != 'audio_listening':
            text(row.get('limitations'), 'Limites de la comparaison sans écoute')
    text(report.get('reason'), 'Choix de la prise')
    text(report.get('raccord_review'), 'Continuité et raccords')
    references = {r['review_id']: r for r in approved_voice_references(pipeline.editorial, pipeline)}
    compared = report.get('reference_comparisons', [])
    if references and not compared:
        raise ValueError('Comparer la prise à une référence réellement validée.')
    for row in compared:
        reference = references.get(row.get('review_id'))
        if not reference or row.get('sha256') != reference['sha256']:
            raise ValueError('Référence vocale non validée par le propriétaire.')
        text(row.get('observation'), 'Comparaison à la référence')
    return report


def selection_report(pipeline, story):
    audition = story.get('audition', {})
    path = audition.get('selection_path')
    if not path:
        raise ValueError('Comparaison des prises manquante : génération conservée sans livraison.')
    report = pipeline.store.read(path)
    if not report or digest(report) != audition.get('selection_sha256'):
        raise ValueError('Compte rendu de sélection vocale absent ou modifié.')
    return validate_voice_selection(pipeline, story, audition, report)


def record_montage_review(pipeline, project_id, run_id, report):
    from app.services.editorial_inbox import EditorialInbox
    if any(r['kind']=='run' and r['target_id']==run_id and r['decision']=='keep' for r in EditorialInbox(pipeline.editorial,pipeline).list()):
        raise ValueError('Un Reel validé reste immuable ; revue réservée à une nouvelle proposition.')
    project = pipeline.read(project_id)
    run = next(r for r in project['runs'] if r['id'] == run_id)
    validate_montage_review(pipeline, run, report)
    relative = 'projects/' + project_id + '/artistic-reviews/' + digest(report) + '.json'
    pipeline.store.write(relative, report, immutable=True)
    # The report is an assistant proposal, not an owner approval or a change to media.
    run['artistic_review_path'] = relative
    run['artistic_review_sha256'] = digest(report)
    pipeline.store.write(pipeline.path(project_id), project)
    return report


def validate_montage_review(pipeline, run, report):
    if report.get('run_id') != run['id'] or report.get('video_sha256') != run['sha256']:
        raise ValueError('Revue artistique liée à une autre vidéo.')
    if report.get('review_method') not in {'continuous_av', 'sampled_frames_and_audio', 'sampled_frames_and_signal'}:
        raise ValueError('Préciser la méthode réelle de revue du montage.')
    text(report.get('coverage'), 'Couverture du visionnage')
    if report['review_method'] != 'continuous_av':
        text(report.get('limitations'), 'Limites du visionnage échantillonné')
    for key in MONTAGE_DIMENSIONS:
        text(report.get('observations', {}).get(key), 'Montage / ' + key)
    timeline = run.get('timeline', [])
    shots = report.get('shots', [])
    if not timeline or len(shots) != len(timeline):
        raise ValueError('Revoir chaque plan de la chronologie exacte.')
    for index, (shot, actual) in enumerate(zip(shots, timeline)):
        if shot.get('index') != index or shot.get('asset_path') != actual.get('asset'):
            raise ValueError('La sélection examinée diffère du montage.')
        exact_file(pipeline.store, {'file_path': shot['asset_path'], 'sha256': shot.get('asset_sha256')})
        for key in ('purpose', 'composition', 'relation_to_words', 'duration_reason'):
            text(shot.get(key), 'Plan / ' + key)
        candidates = shot.get('candidates', [])
        if not candidates or (index in (0, len(shots)-1) and len(candidates) < 2):
            raise ValueError('Comparer au moins deux images pour l’ouverture et la fin.')
        if len({c.get('sha256') for c in candidates}) != len(candidates) or shot['asset_sha256'] not in {c.get('sha256') for c in candidates}:
            raise ValueError('Candidats visuels distincts et image retenue requis.')
        for candidate in candidates:
            exact_file(pipeline.store, candidate)
            text(candidate.get('reason'), 'Choix / rejet du candidat visuel')
    duration = run.get('technical', {}).get('duration')
    for issue in report.get('issues', []):
        seconds = issue.get('seconds')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or duration is None or not 0 <= seconds <= duration:
            raise ValueError('Instant du défaut invalide.')
        text(issue.get('observation'), 'Défaut du montage')
        if issue.get('status') not in {'fixed', 'minor_remaining'}:
            raise ValueError('Défaut bloquant non corrigé.')
    text(report.get('comparison'), 'Comparaison à la version précédente ou référence')
    return report


def montage_report(pipeline, run):
    report = pipeline.store.read(run.get('artistic_review_path', 'missing-artistic-review.json'))
    if not report or digest(report) != run.get('artistic_review_sha256'):
        raise ValueError('Revue artistique du fichier livré manquante ou modifiée.')
    return validate_montage_review(pipeline, run, report)


def benchmark_insight(value):
    """A hypothesis needs examples AND counterexamples, not an invented viral score."""
    for key in ('mechanism', 'performance_context', 'limits', 'experiment'):
        text(value.get(key), 'Benchmark / ' + key)
    for key in ('examples', 'counterexamples'):
        rows = value.get(key)
        if not isinstance(rows, list) or not rows:
            raise ValueError('Exemples et contre-exemples documentés requis.')
        for row in rows:
            for field in ('source', 'observed_at', 'observation'):
                text(row.get(field), 'Référence / ' + field)
    if value.get('causality_proven') is not False:
        raise ValueError('Le benchmark fournit une hypothèse, pas une causalité prouvée.')
    return value


def record_benchmark_insight(workflow, value):
    benchmark_insight(value)
    verify_benchmark_evidence(workflow,value)
    key = digest(value)
    saved = dict(value, id=key, recorded_at=now(), status='hypothesis_to_test', author='assistant')
    path = 'benchmark-insights/' + key + '.json'
    existing = workflow.store.read(path)
    if existing:
        return existing
    workflow.store.write(path, saved, immutable=True)
    return saved


def verify_benchmark_evidence(workflow,value):
    for ref in value.get('evidence_refs',[]):
        document=workflow.store.read(ref['path'])
        if not document or document.get('source')!=ref['source'] or digest(document)!=ref['document_sha256']:
            raise ValueError('Preuve du benchmark absente ou modifiée.')


def usable_benchmark(workflow):
    insights,invalid=[],[]
    for path in (workflow.store.root/'benchmark-insights').glob('*.json'):
        value=json.loads(path.read_text())
        try:
            benchmark_insight(value);verify_benchmark_evidence(workflow,value)
            insights.append(value)
        except (ValueError,KeyError,OSError):invalid.append(path.stem)
    return insights,invalid


def current_voice_approved(workflow, pipeline, story):
    from app.services.editorial_inbox import EditorialInbox
    # A visual correction to a Reel must not reopen its exact accepted narration.
    # Reopening from a Reel requires an explicit voice/text correction category.
    reviews = [r for r in EditorialInbox(workflow, pipeline).list()
               if r['place_id'] == story['place_id'] and r.get('actor') == workflow.read()['owner_id']
               and (r['kind'] == 'story' or
                    (r.get('correction') or {}).get('category') in {'text', 'pronunciation', 'rhythm'})]
    if reviews and reviews[-1]['decision'] != 'keep':
        return False  # A genuine later correction reopens this proposal, not the historical approval.
    return any(r['story_id'] == story['id'] and r['sha256'] == story.get('audition', {}).get('sha256')
               for r in approved_voice_references(workflow, pipeline))


def generation_learning(workflow):
    """Bring observed outcomes and benchmark hypotheses back into the next writing packet."""
    from app.services.studio_performance import performance_view
    observations = []
    for path in (workflow.store.root / 'reels/published').glob('*.json'):
        publication = json.loads(path.read_text())
        if not publication.get('published'):
            continue
        view = performance_view(publication)
        run = publication.get('snapshot', {}).get('run', {})
        observations.append({'place_id': publication['place_id'], 'run_id': publication['run_id'],
                             'duration': run.get('technical', {}).get('duration'),
                             'records': view['records'], 'platform_verified': False})
    insights,invalid = usable_benchmark(workflow)
    from app.services.production_chain import ProductionChain
    return {'production_observations':ProductionChain(workflow).learning(),
            'publication_observations': observations, 'benchmark_hypotheses': insights,'invalid_benchmark_evidence':invalid,
            'causality_proven': False, 'instructions':
            'Une seule variable par essai, comparer âge/durée/sujet/audience. Données absentes = inconnues. '
            'Les hypothèses ne deviennent pas des préférences du propriétaire ni des faits sur un lieu.'}
