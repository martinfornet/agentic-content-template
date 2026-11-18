"""Explicit local acquisition invocation. No background orchestration or analysis."""
import argparse
import json
import os
from pathlib import Path
import sys

from app.models.acquisition import AcquisitionFailure
from app.services.brand_config import load_brand_config
from app.services.relevance_config import load_relevance_config
from app.services.brand_intelligence import build_brand_intelligence
from app.services.acquisition_config import load_acquisition_config
from app.services.acquisition_http import JsonHttp
from app.services.acquisition_store import AcquisitionStore
from app.services.acquisition_media import MediaDownloader
from app.services.apify_instagram import ApifyInstagramProvider
from app.services.discovery import GeminiRelevanceEvaluator
from app.services.social_acquisition import SocialAcquisitionService


def load_local_env(path):
    """Simple KEY=value file; never execute shell text or overwrite the environment."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.replace("_", "").isalnum():
            raise ValueError("Invalid local environment file.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def reconcile_provider_usage(summary, store, provider):
    """Operational billing read, separate from immutable acquisition evidence."""
    attempt_ids = set(summary.get("attempts", []))
    jobs = []
    for path in (store.root / "runs").glob("*/attempts/*.json"):
        attempt = json.loads(path.read_text())
        if attempt["id"] in attempt_ids and attempt.get("provider_job_id"):
            jobs.append(attempt["provider_job_id"])
    if jobs:
        report = provider.report_usage(jobs)
        summary.setdefault("provider_cost_usd_at_acquisition", summary.get("provider_cost_usd"))
        summary["provider_cost_usd"] = report["reported_cost_usd"]
        summary["provider_usage_report"] = report
        store.write(f"runs/{summary['run_id']}/summary.json", summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", default="brands/example")
    parser.add_argument("--output", default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--live-validation", action="store_true", help="Bounded real-provider run; does not perform S02 analysis.")
    parser.add_argument("--seed-only", action="store_true", help="Skip query discovery for a seed-chain validation; related-source discovery remains enabled.")
    parser.add_argument("--cleanup", action="store_true", help="Remove expired released local reference files; no provider calls.")
    args = parser.parse_args(argv)
    try:
        brand_path = Path(args.brand)
        brand = load_brand_config(brand_path / "brand.yaml")
        context = build_brand_intelligence(brand, load_relevance_config(brand_path / "relevance.json", expected_brand_id=brand.id))
        config = load_acquisition_config(brand_path / "acquisition.json", context)
        if args.seed_only:
            if not config.seed_accounts:
                raise ValueError("Seed-only acquisition requires configured seed accounts.")
            config.queries = []
        store = AcquisitionStore(args.output or f"output/{brand.id}/social")
        downloader = MediaDownloader(store, max_bytes=config.max_media_bytes, timeout=config.http_timeout_seconds)
        if args.cleanup:
            manifest = store.read("manifest.json")
            if manifest and manifest.get("brand_id") != brand.id:
                raise ValueError("Cleanup storage root belongs to another brand.")
            print(json.dumps({"removed_media": downloader.cleanup(config.retention_days)}))
            return 0
        load_local_env(args.env_file)
        missing = [key for key in ("APIFY_TOKEN", "GEMINI_API_KEY") if not os.environ.get(key)]
        if missing:
            print(json.dumps({"status": "blocked", "missing_credentials": missing,
                              "live_provider_validated": False, "media_attempts": 0}), file=sys.stderr)
            return 2
        if args.live_validation:
            config.max_accounts, config.new_accounts_per_day = 8, 8
            config.recent_per_account, config.history_per_account = 3, 3
            config.media_per_day, config.queries_per_run = 5, max(1, min(4, len(config.queries)))
            config.results_per_query = 10
        http = JsonHttp(config.http_timeout_seconds, config.retries)
        provider = ApifyInstagramProvider(os.environ["APIFY_TOKEN"], http=http, include_shares=config.include_shares,
                                         wait_seconds=config.provider_wait_seconds, poll_seconds=config.provider_poll_seconds)
        evaluator = GeminiRelevanceEvaluator(os.environ["GEMINI_API_KEY"], config.relevance_model, http)
        summary = SocialAcquisitionService(context, config, provider, evaluator, store, downloader).run()
        reconcile_provider_usage(summary, store, provider)
        summary["live_provider_validated"] = False
        if args.live_validation:
            summary["validation_status"] = "requires_review"
            summary["validation_requirements"] = "Review 5-10 relevant accounts, recent samples, discovery beyond seeds, five media attempts and playable successes."
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except AcquisitionFailure as exc:
        print(json.dumps({"status": "failed", "error": exc.error.to_dict(), "live_provider_validated": False}), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "configuration_error", "message": "Check local configuration, environment file and paths."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
