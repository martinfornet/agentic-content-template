"""Editorial direction drives research intentions and evidence-based qualification.

No model/provider calls. Questions guide discovery; semantic judgments remain
explicit, attributed reviews rather than keyword-based automatic acceptance.
"""
from copy import deepcopy
import json
from pathlib import Path

from app.models.ecosystem import SOURCE_FAMILIES
from app.models.brand_intelligence import BrandIntelligence
from app.services.ecosystem_parse import digest


def build_research_plan(context: BrandIntelligence, direction: dict) -> dict:
    if direction.get('schema_version') != 1 or direction.get('brand_id') != context.brand_id:
        raise ValueError('Editorial direction version/brand mismatch.')
    guidance = context.research_context()
    if any(not guidance['identity'].get(field) for field in ('mission', 'positioning', 'audience')):
        raise ValueError('Research requires defined mission, positioning and audience.')
    intents = direction.get('intents')
    if not isinstance(intents, list) or not intents:
        raise ValueError('Editorial research requires explicit intentions.')
    seen = set()
    for intent in intents:
        for field in ('id', 'question', 'reader_value', 'evidence_needed'):
            if not isinstance(intent.get(field), str) or not intent[field].strip():
                raise ValueError('Editorial intention requires ' + field)
        if not isinstance(intent.get('priority', 1), int) or isinstance(intent.get('priority', 1), bool) or not 1 <= intent.get('priority', 1) <= 3:
            raise ValueError('Research priority must be an integer between 1 and 3.')
        if intent['id'] in seen:
            raise ValueError('Duplicate editorial intention.')
        seen.add(intent['id'])
        domains = intent.get('domain_ids')
        if not isinstance(domains, list) or not domains:
            raise ValueError('Intention must reference relevance domains.')
        for domain in domains:
            if context.is_domain_excluded(domain):
                raise ValueError('Excluded domain cannot direct research.')
        families = intent.get('source_families')
        if not isinstance(families, list) or not families or not set(families) <= SOURCE_FAMILIES:
            raise ValueError('Invalid editorial source families.')
        for field in ('queries', 'relationships'):
            if not isinstance(intent.get(field), list) or not intent[field] or not all(
                    isinstance(x, str) and x.strip() for x in intent[field]):
                raise ValueError('Intention requires ' + field)
        if not set(intent['relationships']) <= {'linked_mentions', 'client_references', 'official_sources', 'feeds'}:
            raise ValueError('Unknown research relationship.')
    checks = direction.get('selection_checks')
    if not isinstance(checks, list) or not checks or not all(isinstance(x, str) and x.strip() for x in checks):
        raise ValueError('Editorial selection checks required.')
    plan = {'schema_version': 1, 'brand_id': context.brand_id, 'guidance': guidance,
            'scope': deepcopy(direction.get('scope', {})), 'intents': deepcopy(intents),
            'selection_checks': deepcopy(checks), 'reference_status': direction.get('reference_status', 'not_supplied')}
    plan['fingerprint'] = digest(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return plan


def load_research_plan(directory, *, expected_brand_id):
    # Existing Brand Brain remains the source of mission/audience/editorial rules.
    from app.services.brand_config import load_brand_config
    from app.services.relevance_config import load_relevance_config
    from app.services.brand_intelligence import build_brand_intelligence
    directory = Path(directory)
    brand = load_brand_config(directory / 'brand.yaml')
    if brand.id != expected_brand_id:
        raise ValueError('Research brand mismatch.')
    context = build_brand_intelligence(brand, load_relevance_config(directory / 'relevance.json', expected_brand_id=brand.id))
    return build_research_plan(context, json.loads((directory / 'editorial-research.json').read_text()))


def intents_for_family(plan, family):
    # These are research assignments, never a claim that a document answers them.
    return [intent['id'] for intent in plan['intents'] if family in intent['source_families']]


def validate_editorial_review(plan, record, candidate, store):
    assessment = record.get('editorial_assessment')
    if not isinstance(assessment, dict):
        raise ValueError('Subject/publication acceptance requires an editorial assessment.')
    if assessment.get('plan_fingerprint') != plan['fingerprint']:
        raise ValueError('Editorial assessment must use the current Brand Brain research plan.')
    intent_ids = assessment.get('intent_ids')
    if not isinstance(intent_ids, list) or not intent_ids or not set(intent_ids) <= {i['id'] for i in plan['intents']}:
        raise ValueError('Editorial assessment requires configured intention references.')
    for field in ('reader_value', 'distinctive_reason', 'limitations'):
        if not isinstance(assessment.get(field), str) or not assessment[field].strip():
            raise ValueError('Editorial assessment requires ' + field)
    if assessment.get('selection_checks') != plan['selection_checks']:
        raise ValueError('Review must explicitly address current editorial selection checks.')
    reasoning = assessment.get('criteria_reasoning')
    if not isinstance(reasoning, list) or len(reasoning) != len(plan['selection_checks']) or not all(
            isinstance(x, str) and x.strip() for x in reasoning):
        raise ValueError('Each editorial selection check requires explicit reasoning.')
    proofs = assessment.get('evidence')
    if not isinstance(proofs, list) or not proofs:
        raise ValueError('Editorial assessment requires original evidence excerpts.')
    for proof in proofs:
        reference = proof.get('source_id')
        excerpt = proof.get('excerpt', '')
        if reference not in candidate['evidence_references'] or not isinstance(excerpt, str) or not excerpt.strip():
            raise ValueError('Editorial evidence must belong to the candidate.')
        source = store.read(f'sources/{reference}.json')
        if not source or ' '.join(excerpt.split()).casefold() not in ' '.join(source['extracted_content'].split()).casefold():
            raise ValueError('Editorial excerpt is not present in original extracted evidence.')
    return deepcopy(assessment)
