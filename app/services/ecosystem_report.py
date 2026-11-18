"""Human-inspectable acquisition review; not the final EI02 Intelligence Report."""
from html import escape
from collections import Counter


def write_review(store):
    state = store.read('ecosystem/registry.json')
    if not state:
        raise ValueError('No ecosystem registry.')
    rows = []
    for candidate in state['candidates'].values():
        evidence = ' '.join(f'<a href="../sources/{escape(ref)}.json">preuve {i+1}</a>'
                            for i, ref in enumerate(candidate['evidence_references']))
        target = candidate.get('identity_url')
        title = escape(candidate.get('display_title', candidate['title']))
        if target:
            title = f'<a href="{escape(target, quote=True)}">{title}</a>'
        review = candidate.get('reviews', [{}])[-1]
        reviewer = review.get('reviewer', 'aucune revue')
        qualified = candidate.get('record_kind', 'unknown')
        if candidate.get('same_subject_as'):
            qualified += ' / même sujet que ' + candidate['same_subject_as']
        editorial = candidate.get('editorial_reviews', [{}])[-1]
        editorial_status = candidate.get('editorial_status', 'non évalué')
        if candidate.get('editorial_plan_fingerprint') != state.get('research_plan_fingerprint'):
            editorial_status = 'needs_review / direction mise à jour'
        editorial_detail = 'Intentions de recherche : ' + ', '.join(candidate.get('research_intent_ids', []))
        if editorial.get('reader_value'):
            editorial_detail += ' — Public : ' + editorial['reader_value'] + ' — Intérêt : ' + editorial['distinctive_reason']
        editorial_detail += ' — Évaluation par : ' + editorial.get('reviewer', 'aucune')
        editorial_detail += ' — Limites : ' + editorial.get('limitations', 'à examiner')
        if editorial.get('criteria_reasoning'):
            editorial_detail += ' — Critères : ' + '; '.join(editorial['criteria_reasoning'])
        if editorial.get('evidence'):
            evidence += ''.join('<blockquote>' + escape(proof['excerpt']) + '</blockquote>'
                                for proof in editorial['evidence'])
        rows.append('<tr><td>' + escape(qualified) + '</td><td>' + title + '</td><td>' + escape(candidate['subject_type']) + '</td><td>' +
                    escape(str(candidate.get('start_date') or 'inconnue')) + ' → ' +
                    escape(str(candidate.get('end_date') or 'inconnue')) + '</td><td>' +
                    escape(candidate['status']) + ' / ' + escape(reviewer) + '<br>Éditorial : ' + escape(editorial_status) +
                    '<br>' + escape(editorial_detail) + '</td><td>' + escape(candidate.get('review_rationale', candidate['rationale'])) + '<br>' +
                    escape('; '.join(candidate['uncertainty'])) + '<br>Prochaine vérification : ' +
                    escape(candidate.get('next_action', 'à préciser')) + '</td><td>' + evidence + '</td></tr>')
    sources = ''.join('<li><a href="' + escape(s['url'], quote=True) + '">' + escape(s['name']) +
                      '</a> — ' + escape(s['family']) + ' / ' + escape(s['access_status']) +
                      ' / ' + escape(s['decision']) + '</li>' for s in state['sources'].values())
    plan_link = '<p><a href="research-plans/' + escape(state.get('research_plan_fingerprint', 'missing')) + '.json">Direction éditoriale et plan de recherche versionné</a></p>'
    html = '''<!doctype html><html lang="fr"><meta charset="utf-8"><title>Revue acquisition</title>
    <style>body{font-family:system-ui;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.6rem;vertical-align:top}</style>
    <h1>Revue technique acquisition</h1><p>Candidats à examiner, pas découvertes pertinentes déjà validées.
    Les pages d'accueil et publications ne sont pas automatiquement des sujets résolus. Les liens sociaux sont contextuels.
    Revue assistant et acceptation humaine sont distinctes ; une qualification pertinente ne vérifie pas les claims. Aucune indépendance de confirmation présumée. Ce document ne remplace pas le rapport de référence ni son évaluation humaine.</p>
    <table><tr><th>Qualification</th><th>Candidat / source originale</th><th>Type</th><th>Dates attribuées</th><th>Revue</th><th>Raison et limites</th><th>Preuves originales</th></tr>'''
    kinds = Counter(c.get('record_kind', 'unknown') for c in state['candidates'].values())
    unique_subjects = sum(c.get('record_kind') == 'subject' and c['status'] == 'accepted'
                          and not c.get('same_subject_as') for c in state['candidates'].values())
    editorial_subjects = sum(c.get('record_kind') == 'subject' and c.get('editorial_status') == 'accepted'
                             and c.get('editorial_plan_fingerprint') == state.get('research_plan_fingerprint')
                             and not c.get('same_subject_as') for c in state['candidates'].values())
    counts = ('<p>Sujets retenus éditorialement (revues attribuées, pas acceptation produit humaine) : ' + str(editorial_subjects) +
        '. Sujets qualifiés côté acquisition hors doublons explicites : ' + str(unique_subjects) + '. Types de dossiers : ' + escape(', '.join(f'{k}: {v}' for k, v in sorted(kinds.items()))) + '</p>')
    target = store.path('ecosystem/review.html')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html.replace('<table>', plan_link + '<table>', 1) + ''.join(rows) + '</table>' + counts + '<h2>Registre des sources/pages</h2><ul>' + sources + '</ul></html>', encoding='utf-8')
    return target
