"""Parent-owned restart journal and evidence-backed repair plans. No approvals."""
import copy
import json
from pathlib import Path
from app.services.studio_tasks import digest, now

MAX_REVIEWS = 3


def issue_rows(report):
    return [dict(i, issue_id=digest(i)) for i in report.get('issues', [])]


def facts(packet):
    run = packet.get('run')
    if run:
        timeline = run.get('timeline', [])
        assets = run.get('inputs', {}).get('assets', [])
        if isinstance(assets, dict): assets = list(assets.values())
        narration = run.get('approved_narration') or run.get('inputs', {}).get('approved_narration', {})
        return {'media': run['sha256'],
                'cuts': digest([(s['start'], s['end']) for s in timeline]),
                'images': digest([(s.get('clip', {}).get('sha256'), s.get('asset_index'), s.get('visual_filter')) for s in timeline]),
                'motion': digest({'photo': run.get('inputs', {}).get('photo_motion', {}),
                                  'video': [a for a in assets if a.get('type') == 'VIDEO']}),
                'captions': run.get('files', {}).get('subtitles.srt'),
                'voice': narration.get('sha256'), 'music': narration.get('music', {}).get('sha256'),
                'text': digest(packet.get('draft'))}
    audio = packet.get('audio') or {}
    files = {Path(f.get('path', '')).name: f.get('sha256') for f in packet.get('files', [])}
    return {'media': audio.get('sha256'), 'voice': audio.get('sha256'),
            'text': digest(packet.get('draft')), 'music': digest(packet.get('proof', {}).get('music_sha256')),
            'storyboard': files.get('storyboard-proposal.json'), 'captions': files.get('captions-proposal.json')}


def components(issue, kind):
    if kind != 'run':
        visual = {'timing', 'image_selection', 'opening', 'visual_coherence', 'progression', 'atmosphere', 'visit_appeal', 'ending'}
        if issue['dimension'] in visual: return ['storyboard', 'voice', 'text']
        if issue['dimension'] in {'subtitles', 'punctuation'}: return ['captions']
        return ['music'] if issue['dimension'] == 'mix' else ['voice', 'text']
    return {'timing': ['cuts', 'motion'], 'subtitles': ['captions'], 'mix': ['voice', 'music'],
            'image_selection': ['images', 'cuts'], 'opening': ['images', 'cuts'],
            'visual_coherence': ['images', 'cuts', 'captions'], 'progression': ['images', 'cuts', 'captions'],
            'atmosphere': ['images', 'motion'], 'visit_appeal': ['images', 'cuts'],
            'ending': ['images', 'cuts', 'captions']}.get(issue['dimension'], ['images', 'cuts', 'captions'])


class RevisionProgress:
    def __init__(self, root, events=(), *, journal=None, history_roots=()):
        self.root = Path(root); self.path = Path(journal or root)/'revision-state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'version': 1, 'reviews': [], 'limit': MAX_REVIEWS, 'retry_tokens': []}
        # Recover file hashes from the immutable reviewed packet for old journals.
        for review in self.state['reviews']:
            if review.get('kind') != 'story' or 'storyboard' in review['facts'] or not review.get('packet_path'): continue
            packet = json.loads(Path(review['packet_path']).read_text())
            if digest(packet) != review['packet_sha256']: raise ValueError('Paquet critique modifié : reprise refusée.')
            review['facts'] = facts(packet)
        # Import only reports recorded by the parent chain, never arbitrary producer JSON.
        receipts = {e['evidence'].get('packet_sha256'): e['evidence'] for e in events if e['step'] == 'independent_review'}
        known = {r['packet_sha256'] for r in self.state['reviews']}
        packets = {p for base in {self.root, *map(Path, history_roots)} for p in base.glob('critic/*/packet.json')}
        def order(p):
            key=digest(json.loads(p.read_text()))
            return (receipts.get(key, {}).get('reviewed_at') or '', str(p))
        packets = sorted(packets, key=order)
        for p in packets:
            packet = json.loads(p.read_text()); key = digest(packet)
            if key in receipts and key not in known:
                self.add(packet, receipts[key], save=False, packet_path=p); known.add(key)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2)); tmp.replace(self.path)

    def add(self, packet, receipt, *, save=True, packet_path=None):
        key = digest(packet)
        if any(r['packet_sha256'] == key for r in self.state['reviews']): return
        self.state['reviews'].append({'packet_sha256': key, 'facts': facts(packet), 'kind': packet['kind'],
                                      'report': copy.deepcopy(receipt['report']), 'reviewed_at': receipt.get('reviewed_at'),
                                      'receipt': copy.deepcopy(receipt), 'packet_path': str(packet_path) if packet_path else None})
        self.state['phase'] = 'reviewed' if receipt['report']['verdict'] == 'pass' else 'repair_required'
        if save: self.save()

    @property
    def latest(self):
        return self.state['reviews'][-1] if self.state['reviews'] else None

    def allow_manual_retry(self, token):
        # A deliberate retry buys ONE extra review, not another automatic three-round loop.
        if token and token not in self.state['retry_tokens']:
            self.state['retry_tokens'].append(token)
            self.state['limit'] = max(self.state['limit'], len(self.state['reviews']) + 1)
            self.save()

    def require_budget(self):
        if len(self.state['reviews']) >= self.state['limit']:
            raise ValueError('Revue indépendante : reprises automatiques épuisées. Le diagnostic et les fichiers sont conservés ; une reprise ciblée doit traiter les points restants.')

    def passed_receipt(self):
        latest=self.latest
        if not latest or latest['report']['verdict']!='pass' or not latest.get('packet_path'):return None
        from app.services.studio_critic import verify_files
        packet=json.loads(Path(latest['packet_path']).read_text())
        if digest(packet)!=latest['packet_sha256']:raise ValueError('Paquet critique accepté modifié : reprise refusée.')
        verify_files(packet)
        return {'packet':packet,'receipt':latest['receipt']}

    def context(self, frozen, scope):
        prior = self.latest
        context = dict(frozen, correction_scope=scope,
                       revision_history=[{'review': i+1, 'media': r['facts'].get('media'), 'verdict': r['report']['verdict'],
                                          'summary': r['report']['summary'], 'issues': issue_rows(r['report'])}
                                         for i, r in enumerate(self.state['reviews'])])
        if prior: context['latest_critique'] = prior['report']
        return context

    def contract(self, task, scope):
        prior = self.latest
        rejected = prior and prior['report']['verdict'] != 'pass'
        issues = issue_rows(prior['report']) if rejected else []
        return {'version': 1, 'task_id': task['id'], 'scope': scope,
                'request': scope['original_request'], 'baseline': prior['facts'] if rejected else None,
                'issues': [dict(i, evidence_components=components(i, prior['kind'])) for i in issues],
                'required_issue_ids': [i['issue_id'] for i in issues if i['severity'] == 'blocking'],
                'review_count': len(self.state['reviews']), 'review_limit': self.state['limit']}

    def check_candidate(self, packet, plan):
        current = facts(packet)
        for review in self.state['reviews']:
            if review['facts'].get('media') != current.get('media') or review['report']['verdict'] == 'pass': continue
            blocking = [i for i in issue_rows(review['report']) if i['severity'] == 'blocking']
            def fixed_without_audio(issue):
                permitted = set(components(issue, packet['kind'])) & {'storyboard', 'captions', 'music'}
                return any(current.get(k) is not None and review['facts'].get(k) is not None and current[k] != review['facts'][k] for k in permitted)
            if packet['kind'] != 'story' or not blocking or not all(fixed_without_audio(i) for i in blocking):
                raise ValueError('La vidéo ou voix est identique à une version refusée : corriger avant une nouvelle revue.')
        prior = self.latest
        if not prior or prior['report']['verdict'] == 'pass': return []
        changed = [k for k, v in current.items() if k != 'media' and v != prior['facts'].get(k)]
        evidence = []
        actions = {r['issue_id']: r for r in plan['repairs']}
        for issue in issue_rows(prior['report']):
            if issue['severity'] != 'blocking': continue
            affected = sorted(set(changed) & set(components(issue, packet['kind'])))
            if not affected:
                raise ValueError('Correction non démontrée pour '+issue['dimension']+' : les éléments concernés sont inchangés.')
            evidence.append({'issue_id': issue['issue_id'], 'action': actions[issue['issue_id']]['action'],
                             'changed_components': affected,
                             'before': {k: prior['facts'].get(k) for k in affected},
                             'after': {k: current.get(k) for k in affected},
                             'semantic_resolution_requires_independent_review': True})
        return evidence


def validate_plan(contract, plan):
    if not isinstance(plan, dict) or plan.get('contract_sha256') != digest(contract):
        raise ValueError('Plan de correction absent ou périmé : lire revision-contract.json et enregistrer revision-plan.json avant production.')
    if not isinstance(plan.get('owner_request_response'), str) or len(plan['owner_request_response'].strip()) < 15:
        raise ValueError('Le plan doit expliquer comment il traite la demande du propriétaire.')
    rows = plan.get('repairs', [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): raise ValueError('Liste de reprises invalide.')
    ids = [r.get('issue_id') for r in rows]
    if len(ids) != len(set(ids)) or not set(contract['required_issue_ids']) <= set(ids):
        raise ValueError('Chaque défaut bloquant doit avoir une reprise ciblée dans le plan.')
    known = {i['issue_id'] for i in contract['issues']}
    if not set(ids) <= known: raise ValueError('Défaut inventé dans le plan de reprise.')
    if any(not isinstance(r.get('action'), str) or len(r['action'].strip()) < 15 for r in rows):
        raise ValueError('Décrire une action concrète pour chaque reprise.')
    return plan


def require_plan(workflow):
    contract = workflow.store.read('revision-contract.json')
    if not contract: return None
    return validate_plan(contract, workflow.store.read('revision-plan.json'))
