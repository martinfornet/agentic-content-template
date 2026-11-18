"""Resumable multi-source discovery; lexical triage never certifies product relevance."""
from datetime import UTC, datetime, timedelta
import json
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import quote_plus, urlsplit
from uuid import uuid4

from app.models.acquisition import now
from app.models.ecosystem import EcosystemSource, SubjectCandidate, SOURCE_FAMILIES
from app.models.source import Source, SourceType
from app.services.acquisition_store import AcquisitionStore, stable_key
from app.services.ecosystem_http import AccessFailure, PublicWeb
from app.services.ecosystem_parse import canonical_url, digest, parse_document, SOCIAL_HOSTS
from app.services.editorial_research import load_research_plan, intents_for_family, validate_editorial_review


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config.get('schema_version') != 1 or not config.get('brand_id'):
        raise ValueError('Invalid ecosystem configuration.')
    for field in ('seeds',):
        if not isinstance(config.get(field), list):
            raise ValueError(field + ' must be a list.')
    config.setdefault('searches', [])
    for field in ('max_pages', 'max_links_per_page', 'revisit_hours'):
        if not isinstance(config.get(field), int) or config[field] < 1:
            raise ValueError(field + ' must be positive.')
    if config.get('editorial_direction'):
        if config['editorial_direction'] != 'editorial-research.json':
            raise ValueError('Editorial direction must be in the brand directory.')
        config['research_plan'] = load_research_plan(Path(path).parent, expected_brand_id=config['brand_id'])
    return config


def triage(text, config):
    text = text.casefold()
    geo = [word for word in config.get('geography_terms', []) if word.casefold() in text]
    topics = [word for word in config.get('topics', []) if word.casefold() in text]
    if geo and topics:
        return 'candidate', 'Configured geography/topic indicators: ' + ', '.join(geo[:2] + topics[:4]) + '; human relevance review required.'
    return 'uncertain', 'No configured geography/topic intersection; unknown location or distinctive interest requires human review.'


class EcosystemCollector:
    def __init__(self, store, config, transport=None, social_store=None):
        self.store, self.config = store, config
        self.research_plan = deepcopy(config.get("research_plan"))
        if not self.research_plan:
            raise ValueError("Ecosystem acquisition requires an editorial Brand Brain research plan.")
        if self.research_plan["brand_id"] != config["brand_id"]:
            raise ValueError("Editorial research brand mismatch.")
        self.social_store = social_store or store
        self.transport = transport or PublicWeb(max_requests=config.get('max_requests', 30))
        self.state = store.read('ecosystem/registry.json', {'schema_version': 1, 'brand_id': config['brand_id'],
                                'sources': {}, 'frontier': {}, 'documents': {}, 'candidates': {}, 'social_links': {}})
        if self.state['brand_id'] != config['brand_id']:
            raise ValueError('Storage brand mismatch.')
        self.run_id = uuid4().hex
        self.report = {'run_id': self.run_id, 'started_at': now(), 'brand_id': config['brand_id'],
                       'adapter_version': 's03-editorial-v1', 'pages': [], 'failures': [], 'searches': [], 'new_sources': [], 'new_candidates': [],
                       'limitations': ['Editorial intentions guide collection; no automatic semantic judgment or product acceptance.',
                                       'Evidence independence unknown unless explicit origin/identical text links it.',
                                       'No social provider calls; social evidence requires existing S01/S02 artifacts.']}

    def save(self):
        if self.research_plan:
            self.report['research_plan_fingerprint'] = self.research_plan['fingerprint']
            self.state['research_plan_fingerprint'] = self.research_plan['fingerprint']
            self.store.write(f"ecosystem/research-plans/{self.research_plan['fingerprint']}.json",
                             self.research_plan, immutable=True)
        self.store.write('ecosystem/registry.json', self.state)
        self.store.write(f'ecosystem/runs/{self.run_id}.json', self.report)

    def enqueue(self, url, name, family='unknown', origin=None, priority=0):
        url = canonical_url(url)
        key = next((k for k, source in self.state['sources'].items()
                    if url == source['url'] or url in source['aliases']), stable_key('web', url))
        origin = deepcopy(origin or {'kind': 'manual'})
        if self.research_plan:
            origin['research_plan_fingerprint'] = self.research_plan['fingerprint']
        if key not in self.state['sources']:
            self.state['sources'][key] = EcosystemSource(key, url, name or url, family,
                                                       origins=[origin]).to_dict()
            self.report['new_sources'].append(key)
        else:
            source = self.state['sources'][key]
            if origin not in source['origins']:
                source['origins'].append(origin)
            if family != 'unknown' and source['family'] == 'unknown':
                source['family'] = family
        if self.research_plan:
            source = self.state['sources'][key]
            explicit = origin.get('intent_ids')
            assignments = explicit if explicit is not None else intents_for_family(self.research_plan, source['family'])
            allowed = {i['id'] for i in self.research_plan['intents']}
            if not isinstance(assignments, list) or not set(assignments) <= allowed:
                raise ValueError('Unknown editorial research intention.')
            source['research_intent_ids'] = sorted((set(source.get('research_intent_ids', [])) & allowed) | set(assignments))
            origin['research_plan_fingerprint'] = self.research_plan['fingerprint']
            source['research_plan_fingerprint'] = self.research_plan['fingerprint']
        if self.research_plan and assignments and priority >= 0:
            priority = max(priority, max(i.get('priority', 1) for i in self.research_plan['intents'] if i['id'] in assignments))
        if key not in self.state['frontier']:
            self.state['frontier'][key] = {'source_id': key, 'url': url, 'priority': priority,
                                          'state': 'pending', 'next_check': None, 'queued_at': now()}
        else:
            self.state['frontier'][key]['priority'] = max(priority, self.state['frontier'][key]['priority'])
        return key

    def bootstrap(self):
        for seed in self.config['seeds']:
            self.enqueue(seed['url'], seed['name'], seed.get('family', 'unknown'), {'kind': 'seed'}, 1)
        searches = self.config['searches']
        if self.research_plan:
            # Queries come from editorial questions, never a generic topic list.
            searches = [{'query': query, 'intent_ids': [intent['id']],
                         'rss_template': self.config.get('search_rss_template'), 'priority': intent.get('priority', 1)}
                        for intent in self.research_plan['intents'] for query in intent['queries']]
            self.report['research_questions'] = self.research_plan['intents']
        for search in searches:
            endpoint = search.get('rss_template')
            if endpoint:
                url = endpoint.replace('{query}', quote_plus(search['query']))
                self.enqueue(url, search['query'], origin={'kind': 'thematic_search', 'query': search['query'], 'results_document': True, 'intent_ids': search.get('intent_ids', [])}, priority=search.get('priority', 1))
                self.report['searches'].append({'query': search['query'], 'url': url, 'mode': 'public_rss', 'intent_ids': search.get('intent_ids', [])})
            else:
                self.report['searches'].append({'query': search['query'], 'mode': 'assisted_import_required',
                                               'intent_ids': search.get('intent_ids', []),
                                               'priority': search.get('priority', 1)})

    def import_search_results(self, data):
        """Explicit external search evidence, never the held-out human benchmark."""
        if not data.get('query') or not data.get('searched_at') or not data.get('provider'):
            raise ValueError('Search import requires query, searched_at and provider.')
        source = Source(self.config['brand_id'], SourceType.NOTE, 'Independent thematic search: ' + data['query'],
                        'search-import:' + digest(json.dumps(data, sort_keys=True)),
                        raw_content=json.dumps(data, ensure_ascii=False), metadata={'kind': 'thematic_search',
                        'provider': data['provider'], 'searched_at': data['searched_at']})
        source.metadata.update(research_plan_fingerprint=self.research_plan['fingerprint'],
                               intent_ids=data.get('intent_ids', []))
        self.store.save_source(source)
        for result in data.get('results', []):
            self.enqueue(result['url'], result.get('title', result['url']), origin={'kind': 'thematic_search',
                         'query': data['query'], 'evidence_id': source.id, 'intent_ids': data.get('intent_ids', [])},
                         priority=max((i.get('priority', 1) for i in self.research_plan['intents']
                                       if i['id'] in data.get('intent_ids', [])), default=2))
        self.save()

    def apply_reviews(self, decisions, *, reviewer):
        """Attributed acquisition qualification, never implicit human product acceptance."""
        if reviewer not in {'assistant', 'human'}:
            raise ValueError('Reviewer must be assistant or human.')
        updated = deepcopy(self.state)
        for record in decisions:
            decision, rationale = record.get('decision'), record.get('rationale', '')
            if decision not in {'accepted', 'rejected', 'needs_review'} or not rationale.strip():
                raise ValueError('Review requires explicit decision and rationale.')
            candidate = updated['candidates'].get(record.get('id'))
            source = updated['sources'].get(record.get('id'))
            target = candidate or source
            if target is None:
                raise ValueError('Unknown review identity.')
            if candidate:
                kind = record.get('record_kind', candidate.get('record_kind', 'unknown'))
                if kind not in {'subject', 'publication', 'source_page', 'index', 'unknown'}:
                    raise ValueError('Unknown record kind.')
                candidate['status'], candidate['record_kind'] = decision, kind
                if self.research_plan and kind in {'subject', 'publication'}:
                    if record.get('editorial_assessment'):
                        assessment = validate_editorial_review(self.research_plan, record, candidate, self.store)
                        candidate.setdefault('editorial_reviews', []).append({
                            **assessment, 'decision': decision, 'reviewer': reviewer, 'reviewed_at': now()})
                        candidate['editorial_status'] = decision
                        candidate['editorial_plan_fingerprint'] = self.research_plan['fingerprint']
                    else:
                        # Acquisition acceptance is not editorial acceptance; retain both.
                        candidate['editorial_status'] = 'needs_review'
                        candidate['editorial_plan_fingerprint'] = self.research_plan['fingerprint']
            else:
                source['decision'] = 'pending' if decision == 'needs_review' else decision
                if record.get('family'):
                    if record['family'] not in SOURCE_FAMILIES:
                        raise ValueError('Unknown source family.')
                    source['family'] = record['family']
            if record.get('same_subject_as'):
                reference = record['same_subject_as']
                if not candidate or reference not in updated['candidates']:
                    raise ValueError('Subject alias must reference an existing candidate.')
                visited = {candidate['id']}
                current = reference
                while current:
                    if current in visited:
                        raise ValueError('Cyclic subject alias rejected.')
                    visited.add(current)
                    current = updated['candidates'][current].get('same_subject_as')
                candidate['same_subject_as'] = reference
            if 'priority' in record:
                priority = record['priority']
                if not isinstance(priority, int) or isinstance(priority, bool) or not -1 <= priority <= 3 or not source:
                    raise ValueError('Source follow-up priority must be an integer between -1 and 3.')
                updated['frontier'][source['id']]['priority'] = priority
            if record.get('retry_now'):
                if not source or source['access_status'] != 'unavailable':
                    raise ValueError('Explicit retry applies only to unavailable sources.')
                updated['frontier'][source['id']]['next_check'] = None
                source['next_check'] = None
            review = {**record, 'reviewer': reviewer, 'reviewed_at': now(), 'scope': 'acquisition_relevance_only'}
            target.setdefault('reviews', []).append(review)
            target['review_rationale'] = rationale
            for field in ('display_title', 'next_action'):
                if field in record:
                    if not isinstance(record[field], str) or not record[field].strip():
                        raise ValueError(field + ' must be nonempty text.')
                    target[field] = record[field]
        self.state = updated
        self.report['reviews'] = {'reviewer': reviewer, 'decisions': decisions, 'scope': 'acquisition_relevance_only'}
        self.save()

    def _candidate(self, item, source_id, document_id, social_urls):
        rationale = 'Editorial research evidence retained; audience value and distinctive interest require attributed assessment.'
        identity = item.get('identity_url')
        dates = [item.get('start_date'), item.get('end_date')]
        # Strong URL+occurrence only; identical names are not sufficient for a merge.
        key = stable_key('subject', json.dumps([identity or document_id + ':' + item['title'], dates], sort_keys=True))
        if key not in self.state['candidates']:
            candidate = SubjectCandidate(key, item['title'], [source_id], rationale,
                                         subject_type=item.get('subject_type', 'unknown'), location=item.get('location'),
                                         start_date=dates[0], end_date=dates[1], identity_url=identity,
                                         social_urls=list(social_urls))
            self.state['candidates'][key] = candidate.to_dict()
            self.report['new_candidates'].append(key)
        else:
            candidate = self.state['candidates'][key]
            if source_id not in candidate['evidence_references']:
                candidate['evidence_references'].append(source_id)
                if self.research_plan:
                    candidate['editorial_status'] = 'needs_review'
            for url in social_urls:
                if url not in candidate['social_urls']:
                    candidate['social_urls'].append(url)
            if candidate['title'] != item['title'] or candidate['location'] != item.get('location'):
                candidate['uncertainty'].append('Conflicting title/location in new evidence ' + source_id)
        candidate = self.state['candidates'][key]
        if self.research_plan:
            candidate['research_intent_ids'] = sorted(set(candidate.get('research_intent_ids', []) +
                                                        item.get('research_intent_ids', [])) &
                                                       {i['id'] for i in self.research_plan['intents']})
            current = self.research_plan['fingerprint']
            if candidate.get('editorial_plan_fingerprint') != current:
                candidate['editorial_status'] = 'needs_review'
                candidate['editorial_plan_fingerprint'] = current
            candidate['editorial_review_required'] = {
                'reader_value': 'Why does this matter to the configured audience?',
                'distinctive_reason': 'What is interesting beyond price, prestige, popularity or category?',
                'evidence_needed': [i['evidence_needed'] for i in self.research_plan['intents']
                                    if i['id'] in candidate['research_intent_ids']],
                'selection_checks': self.research_plan['selection_checks']}
        candidate.setdefault('record_kind', 'subject' if candidate['subject_type'] in
                             {'Event', 'ExhibitionEvent', 'Place', 'Restaurant', 'Hotel', 'Museum', 'TouristAttraction'}
                             else 'publication' if candidate['subject_type'] in
                             {'Article', 'NewsArticle', 'BlogPosting', 'publication'} else 'unknown')
        if social_urls and 'Social links are page-context associations, not verified subject ownership.' not in candidate['uncertainty']:
            candidate['uncertainty'].append('Social links are page-context associations, not verified subject ownership.')
        if not candidate['location'] and 'Location not explicitly structured.' not in candidate['uncertainty']:
            candidate['uncertainty'].append('Location not explicitly structured.')
        if candidate['subject_type'] in {'document_topic', 'publication'}:
            note = 'Document/publication candidate; subject identity and distinctive interest require review.'
            if note not in candidate['uncertainty']:
                candidate['uncertainty'].append(note)
        # Evidence-local social links are not automatically the venue's official account.
        for other in self.state['candidates'].values():
            if other['id'] != key and other['title'].casefold() == candidate['title'].casefold():
                note = 'Possible duplicate requiring review: ' + other['id']
                if note not in candidate['uncertainty']:
                    candidate['uncertainty'].append(note)

    def _ingest(self, key, response):
        url, raw, mime = response['url'], response['raw'], response['mime']
        parsed = parse_document(raw, url, mime)
        if parsed['restricted']:
            raise AccessFailure('meta_noindex_restricted')
        text = parsed['text']
        if not text:
            raise AccessFailure('no_extractable_text')
        content_hash = digest(re.sub(r'\s+', ' ', text).strip())
        document_id = stable_key('document', canonical_url(url))
        source = Source(self.config['brand_id'], SourceType.URL, parsed['title'], url, raw_content=raw,
                        extracted_content=text, id=stable_key('evidence', document_id + digest(raw)),
                        metadata={'adapter': 'public_web', 'mime': mime, 'retrieved_at': now(),
                                  'registry_source_id': key, 'content_hash': content_hash,
                                  'structured': parsed['structured'], 'links': parsed['links'],
                                  'independence': 'unknown', 'verification': 'unverified'})
        previous = self.state['documents'].get(document_id, {})
        same_text = [d['id'] for d in self.state['documents'].values()
                     if d.get('content_hash') == content_hash and d['id'] != document_id]
        origin_links = [link['url'] for link in parsed['links'] if re.search(
            r'communiqu[eé]|press release|press kit|dossier de presse', link['label'], re.I)]
        source.metadata['origin_references'] = origin_links
        source.metadata['identical_text_documents'] = same_text
        if origin_links or same_text:
            source.metadata['independence'] = 'linked_or_identical_origin_not_independent_confirmation'
        if not self.store.read(f"sources/{source.id}.json"):
            self.store.save_source(source)
        references = list(previous.get('evidence_references', []))
        if source.id not in references:
            references.append(source.id)
        for same_id in same_text:
            linked = self.state['documents'][same_id]
            if document_id not in linked['identical_text_documents']:
                linked['identical_text_documents'].append(document_id)
            linked['independence'] = 'linked_or_identical_origin_not_independent_confirmation'
        self.state['documents'][document_id] = {'id': document_id, 'url': url, 'content_hash': content_hash,
                                                'evidence_references': references, 'last_retrieved_at': now(),
                                                'origin_references': origin_links, 'identical_text_documents': same_text,
                                                'independence': source.metadata['independence']}
        social = []
        admitted_links = 0
        registry = self.state['sources'][key]
        if registry['family'] == 'unknown':
            for origin in registry['origins']:
                parent = self.state['sources'].get(origin.get('parent_id'))
                if parent and urlsplit(parent['url']).hostname == urlsplit(url).hostname and parent['family'] != 'unknown':
                    registry['family'] = parent['family']
                    break
        reference_index = (self.state['sources'][key]['family'] == 'pr_agency' and
                           bool(re.search(r'client|reference|r[eé]f[eé]rence', urlsplit(url).path, re.I)))
        source_reason = 'Source assigned to editorial research; relevance not accepted.'
        registry = self.state['sources'][key]
        if url != registry['url'] and url not in registry['aliases']:
            registry['aliases'].append(url)
        if registry['decision'] == 'pending':
            registry['rationale'] = source_reason
        for link in parsed['links']:
            target = link['url']
            if urlsplit(target).hostname in SOCIAL_HOSTS:
                social.append(target)
                registry['social_urls'] = sorted(set(registry['social_urls'] + [target]))
                social_id = stable_key('social', target)
                existing = self.state['social_links'].setdefault(social_id, {'url': target, 'evidence_references': [],
                                                                          'observation_references': []})
                if source.id not in existing['evidence_references']:
                    existing['evidence_references'].append(source.id)
                continue
            # Preserve all links in original evidence; prioritize contextual thematic links.
            # Search/feed outputs and contextually relevant pages can expand outside the seed domain.
            origin_kind = self.state['sources'][key]['origins']
            is_search = any(o.get('results_document') for o in origin_kind)
            if self.research_plan:
                assignments = registry.get('research_intent_ids', [])
                relationships = {rel for intent in self.research_plan['intents']
                                 if intent['id'] in assignments for rel in intent['relationships']}
                if not assignments:
                    # Preserve a narrow route for genuinely unexpected sources, without inventing an axis.
                    relationships = {'feeds', 'official_sources'}
                follow = (is_search or (link['relation'] in {'feed', 'feed_entry'} and 'feeds' in relationships)
                          or (bool(link['label'].strip()) and not link.get('navigation') and
                              ('linked_mentions' in relationships or ('official_sources' in relationships and
                               bool(re.search(r'site officiel|en savoir plus|voir le site|acc[eé]dez au site', link['label'], re.I)))))
                          or (reference_index and link.get('image_label') and 'client_references' in relationships))
                reason = 'Editorial research relationship; document relevance and independence remain unreviewed.'
            if target == canonical_url(url) or link.get('nofollow'):
                continue
            if re.search(r'\.(pdf|jpg|jpeg|png|zip|mp4|webp)$', urlsplit(target).path, re.I):
                continue
            family = registry['family'] if urlsplit(target).hostname == urlsplit(url).hostname else 'unknown'
            if follow and admitted_links < self.config['max_links_per_page']:
                if re.search(r'\.(pdf|jpg|jpeg|png|zip|mp4|webp)$', urlsplit(target).path, re.I):
                    continue
                self.enqueue(target, link['label'], family=family, origin={'kind': 'relation', 'parent_id': key,
                             'evidence_id': source.id, 'relation': link['relation'], 'label': link['label'],
                             'rationale': reason, 'intent_ids': registry.get('research_intent_ids', [])}, priority=-1 if link.get('navigation') else 0)
                admitted_links += 1
            elif follow:
                # Queue all qualifying links: the run bound must not permanently cut expansion.
                self.enqueue(target, link['label'], family=family, origin={'kind': 'relation', 'parent_id': key,
                             'evidence_id': source.id, 'relation': link['relation'], 'label': link['label'], 'intent_ids': registry.get('research_intent_ids', [])}, priority=-1)
        items = parsed['candidates']
        if not items:
            items = [{'title': parsed['title'], 'text': text[:8000], 'identity_url': url,
                      'subject_type': 'document_topic', 'location': None}]
        for item in items:
            item['research_intent_ids'] = registry.get('research_intent_ids', [])
            self._candidate(item, source.id, document_id, social)
        self.report['pages'].append({'url': url, 'source_id': source.id, 'registry_id': key,
                                     'document_id': document_id, 'unchanged': source.id in previous.get('evidence_references', []),
                                     'candidate_items': len(items), 'origin_references': origin_links,
                                     'identical_text_documents': same_text})
        return source.id

    def _link_local_social(self):
        # Existing S01 acquisitions only: no provider invocation and no fabricated analysis.
        for content in self.social_store.contents():
            observation = content.observation
            if content.brand_id != self.config['brand_id']:
                continue
            try:
                url = canonical_url(observation.content_url)
            except (ValueError, AttributeError):
                continue
            for record in self.state['social_links'].values():
                if record['url'] == url and observation.id not in record['observation_references']:
                    record['observation_references'].append(observation.id)
            for candidate in self.state['candidates'].values():
                if url in candidate['social_urls'] and observation.id not in candidate['observation_references']:
                    candidate['observation_references'].append(observation.id)

    def requalify_cached(self):
        """Re-extract actual cached documents under current editorial direction, without network."""
        if not self.research_plan:
            raise ValueError('Cache requalification requires an editorial research plan.')
        self.bootstrap()
        documents = list(self.state['documents'].values())
        for document in documents:
            reference = document['evidence_references'][-1]
            source = self.store.read(f'sources/{reference}.json')
            key = source['metadata']['registry_source_id']
            registry = self.state['sources'][key]
            registry['research_intent_ids'] = sorted(set(registry.get('research_intent_ids', []) +
                                                      intents_for_family(self.research_plan, registry['family'])))
            self._ingest(key, {'url': document['url'], 'raw': source['raw_content'],
                               'mime': source['metadata']['mime']})
        self.report.update(mode='cached_editorial_requalification', http_requests=0, paid_provider_calls=0,
                           completed_at=now(), pending_frontier=sum(
                               e['next_check'] is None for e in self.state['frontier'].values()))
        self.save()
        return self.report

    def run(self):
        self.bootstrap()
        self.save()
        at = datetime.now(UTC)
        frontier = [entry for entry in self.state['frontier'].values()
                    if (not entry['next_check'] or datetime.fromisoformat(entry['next_check']) <= at)
                    and (not any(o.get('results_document') for o in self.state['sources'][entry['source_id']]['origins'])
                         or (self.config.get('search_rss_template') and any(
                             o.get('results_document') and o.get('research_plan_fingerprint') == self.research_plan['fingerprint']
                             for o in self.state['sources'][entry['source_id']]['origins'])))]
        frontier.sort(key=lambda entry: (-entry['priority'], entry['queued_at']))
        # Round-robin hosts within each priority level so one publisher cannot fill a bounded run.
        balanced = []
        for priority in sorted({e['priority'] for e in frontier}, reverse=True):
            hosts = {}
            for entry in frontier:
                if entry['priority'] == priority and self.state['sources'][entry['source_id']]['decision'] != 'rejected':
                    hosts.setdefault(urlsplit(entry['url']).hostname, []).append(entry)
            while any(hosts.values()):
                for entries in hosts.values():
                    if entries:
                        balanced.append(entries.pop(0))
        for entry in balanced[:self.config['max_pages']]:
            key = entry['source_id']
            source = self.state['sources'][key]
            if source['decision'] == 'rejected':
                continue
            result_origins = [o for o in source['origins'] if o.get('results_document')]
            if result_origins and (not self.config.get('search_rss_template') or not any(
                    o.get('research_plan_fingerprint') == self.research_plan['fingerprint'] for o in result_origins)):
                self.report.setdefault('skipped', []).append({'url': entry['url'], 'reason': 'search_not_in_current_plan'})
                continue
            try:
                response = self.transport.fetch(entry['url'])
                self._ingest(key, response)
                source['access_status'] = 'collected'
            except (AccessFailure, OSError, ValueError, LookupError) as exc:
                source['access_status'] = 'unavailable'
                self.report['failures'].append({'url': entry['url'], 'reason': str(exc), 'registry_id': key})
                if str(exc) == 'request_limit':
                    self.save()
                    break
            source['last_checked'] = now()
            source['next_check'] = (datetime.now(UTC) + timedelta(hours=self.config['revisit_hours'])).isoformat()
            entry['next_check'], entry['state'] = source['next_check'], source['access_status']
            self.save()
        self._link_local_social()
        self.report['completed_at'] = now()
        self.report['http_requests'] = getattr(self.transport, 'requests', None)
        self.report['paid_provider_calls'] = 0
        self.report['pending_frontier'] = sum(e['next_check'] is None for e in self.state['frontier'].values())
        self.save()
        return self.report
