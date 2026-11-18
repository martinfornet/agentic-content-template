"""Explicit bounded S02 analysis of existing local acquisition media; no acquisition."""
from dataclasses import asdict
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from app.models.acquisition import AcquisitionFailure, now
from app.services.acquisition_cli import load_local_env
from app.services.acquisition_store import AcquisitionStore
from app.services.brand_config import load_brand_config
from app.services.relevance_config import load_relevance_config
from app.services.brand_intelligence import build_brand_intelligence
from app.services.video_analyzer import GeminiVideoAnalyzer, VideoAnalysisConfig
from app.services.observation_analysis import ObservationAnalysisService, write_safe
from app.services.analysis_limits import AnalysisBudget, STOP_CODES, load_policy
from app.services.analysis_replay import RetainedDecomposedAnalyzer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquisition-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--brand", default="brands/example")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--observation-id", action="append", help="Analyze only these eligible existing IDs; repeat as needed")
    parser.add_argument("--config", help="Operational config; defaults to brands/<brand>/analysis.json")
    parser.add_argument("--policy", help="Free-tier confirmation and limits JSON; defaults to brand analysis-policy.json")
    parser.add_argument("--daily-cap", type=int, help="Override the daily uncached analysis cap; zero disables new analyses")
    parser.add_argument("--usage-root", default="output/gemini-analysis-usage", help="Shared project/model quota ledger")
    parser.add_argument("--force", action="store_true", help="Reanalyze cached content within the same free-only limits")
    parser.add_argument("--replay-input-root", help="Offline replay of retained decomposed input manifests/responses; zero provider requests")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("limit must be positive")
    try:
        load_local_env(args.env_file)
        if args.replay_input_root and args.force:
            parser.error("--force cannot be combined with offline replay")
        if not args.replay_input_root and not os.environ.get("GEMINI_API_KEY"):
            print(json.dumps({"status": "blocked", "missing_credentials": ["GEMINI_API_KEY"]}))
            return 2
        config_path = args.config or args.brand + "/analysis.json"
        config = VideoAnalysisConfig(**json.loads(Path(config_path).read_text()))
        policy = load_policy(args.policy or args.brand + "/analysis-policy.json")
        if args.daily_cap is not None:
            policy.daily_analysis_cap = args.daily_cap
            policy.__post_init__()
        budget = None
        if not args.replay_input_root:
            budget = AnalysisBudget(policy, os.environ["GEMINI_API_KEY"], config.model, args.usage_root)
            # Admission happens inside the adapter after local speech extraction, before any provider request.
        brand = load_brand_config(args.brand + "/brand.yaml")
        context = build_brand_intelligence(brand, load_relevance_config(args.brand + "/relevance.json", expected_brand_id=brand.id))
        acquisition = AcquisitionStore(args.acquisition_root)
        manifest = acquisition.read("manifest.json")
        if not manifest or manifest["brand_id"] != brand.id:
            raise ValueError("Acquisition root does not match the selected brand.")
        output = AcquisitionStore(args.output)
        output.write("manifest.json", {"schema_version": 1, "brand_id": brand.id}, immutable=True)
        analyzer = (RetainedDecomposedAnalyzer(args.replay_input_root, output, config) if args.replay_input_root else
            GeminiVideoAnalyzer(os.environ["GEMINI_API_KEY"], config, file_cache=output,
                reuse_response=not args.force, budget=budget))
        service = ObservationAnalysisService(acquisition, output, analyzer)
        candidates = acquisition.ready_for_analysis(context)
        if args.observation_id:
            wanted = set(args.observation_id)
            if not wanted <= {c.observation.id for c in candidates}:
                raise ValueError("Requested IDs must have eligible acquired local media.")
            candidates = [c for c in candidates if c.observation.id in wanted]
        candidates = candidates[:args.limit]
        report = {"started_at": now(), "config": asdict(config), "acquisition_root": str(acquisition.root),
                  "results": [], "human_review": "pending", "acceptance_validated": False,
                  "billing_policy": "free_only", "daily_analysis_cap": policy.daily_analysis_cap,
                  "billing_verification": "not required for offline replay" if args.replay_input_root else
                      "checked at provider admission; unknown confirmation blocks new calls",
                  "mode": "offline_retained_response" if args.replay_input_root else "live",
                  "provider_requests": 0 if args.replay_input_root else None}
        write_safe(output, "validation-summary.json", report)
        for content in candidates:
            identifier = content.observation.id
            print(json.dumps({"event": "analyzing", "observation_id": identifier, "url": content.observation.content_url}), flush=True)
            try:
                result, cached = service.analyze(content, force=args.force)
                pointer = output.read(f"analyses/{identifier}/latest.json")
                report["results"].append({"observation_id": identifier, "url": content.observation.content_url,
                    "status": result.analysis.status, "cached": cached, "analysis_id": result.analysis.id,
                    "report_path": str(output.path(pointer["report_path"])), "analysis_path": str(output.path(pointer["path"])),
                    "coverage": result.analysis.coverage, "usage": result.analysis.usage, "timings": result.analysis.timings})
            except (AcquisitionFailure, ValueError, KeyError, TypeError, IndexError, OSError, subprocess.SubprocessError) as e:
                report["results"].append({"observation_id": identifier, "url": content.observation.content_url,
                    "status": "failed", "error": e.error.to_dict() if isinstance(e, AcquisitionFailure) else {
                        "code": "analysis_unavailable", "message": "Inspect attempt diagnostics and retained model response."}})
                if service.last_attempt:
                    report["results"][-1].update({k: service.last_attempt[k] for k in
                        ("usage", "elapsed_seconds", "diagnostics", "report_path")})
                if isinstance(e, AcquisitionFailure) and (e.error.code in STOP_CODES or
                        e.error.code in {"authentication_failure", "payment_required"} or e.error.http_status == 429):
                    report["stopped_reason"] = e.error.code
                    if e.error.code == "free_tier_unverified":
                        report["provider_requests"] = 0
                    report["remaining_candidates"] = len(candidates) - len(report["results"])
                    break
            write_safe(output, "validation-summary.json", report)
        report["ended_at"] = now()
        report["analyzed"] = sum(r["status"] in {"completed", "partial"} for r in report["results"])
        report["failed"] = sum(r["status"] == "failed" for r in report["results"])
        costs = [r["usage"]["estimated_cost_usd"] for r in report["results"] if not r.get("cached")
            and not r.get("timings", r.get("diagnostics", {})).get("response_reused")
            and r.get("usage", {}).get("estimated_cost_usd") is not None]
        report["measured_call_estimated_cost_usd"] = sum(costs) if costs else None
        report["cost_note"] = "Current-run measured new calls, including locally rejected responses; cached usage is historical. Unreported charges remain unknown."
        write_safe(output, "validation-summary.json", report)
        print(json.dumps(report, indent=2), flush=True)
        return 0 if candidates and not report["failed"] else 1
    except AcquisitionFailure as e:
        print(json.dumps({"status": "blocked", "reason": e.error.code, "message": e.error.message,
                          "provider_requests": 0}), flush=True)
        return 2
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "configuration_error", "message": "Check analysis configuration, credentials and local paths."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
