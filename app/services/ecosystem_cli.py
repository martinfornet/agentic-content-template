"""Local public ecosystem acquisition; no API credentials or paid calls."""
import argparse
import json
from pathlib import Path

from app.services.acquisition_store import AcquisitionStore
from app.services.ecosystem import EcosystemCollector, load_config
from app.services.ecosystem_report import write_review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='brands/example/ecosystem.json')
    parser.add_argument('--output', default='output/example/ecosystem')
    parser.add_argument('--plan-only', action='store_true', help='Persist editorial research plan without network.')
    parser.add_argument('--requalify-cache', action='store_true', help='Reprocess original evidence without network.')
    parser.add_argument('--search-results', help='Public thematic search results JSON, never the held-out benchmark.')
    parser.add_argument('--social-store', help='Existing S01 storage to link local acquisitions; no provider calls.')
    parser.add_argument('--max-pages', type=int)
    parser.add_argument('--max-requests', type=int)
    parser.add_argument('--reviewer', choices=['assistant', 'human'], help='Required attribution when reviewing.')
    parser.add_argument('--review', help='JSON decisions for sources/candidates: id, decision, rationale.')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if not config.get('research_plan'):
        parser.error('An editorial Brand Brain research plan is required for acquisition.')
    for key in ('max_pages', 'max_requests'):
        value = getattr(args, key)
        if value is not None:
            if value < 1:
                parser.error(key + ' must be positive')
            config[key] = value
    collector = EcosystemCollector(AcquisitionStore(args.output), config,
                                   social_store=AcquisitionStore(args.social_store) if args.social_store else None)
    if args.plan_only:
        collector.save()
        print(json.dumps(collector.research_plan, ensure_ascii=False, indent=2))
        return 0
    if args.review:
        decisions = json.loads(Path(args.review).read_text())
        if not args.reviewer:
            parser.error('--review requires --reviewer assistant or human')
        collector.apply_reviews(decisions, reviewer=args.reviewer)
        write_review(collector.store)
        print(json.dumps({'reviewed': len(decisions), 'paid_provider_calls': 0}))
        return 0
    if args.search_results:
        collector.import_search_results(json.loads(Path(args.search_results).read_text()))
    report = collector.requalify_cached() if args.requalify_cache else collector.run()
    write_review(collector.store)
    print(json.dumps({'run_id': report['run_id'], 'pages_collected': len(report['pages']),
                      'failures': report['failures'], 'new_candidates': len(report['new_candidates']),
                      'pending_frontier': report['pending_frontier'], 'http_requests': report['http_requests'],
                      'paid_provider_calls': 0, 'report': str(collector.store.path(
                          f"ecosystem/runs/{report['run_id']}.json"))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
