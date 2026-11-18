"""Local free-only S02 admission and quota reservations; no media interpretation."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from app.models.acquisition import AcquisitionError, AcquisitionFailure
from app.services.acquisition_store import AcquisitionStore


STOP_CODES = {"free_tier_unverified", "free_tier_required", "daily_analysis_cap", "quota_exhausted"}


def stop(code, message):
    raise AcquisitionFailure(AcquisitionError(code, message))


@dataclass(slots=True)
class FreeTierPolicy:
    model: str = "gemini-3.1-flash-lite"
    daily_analysis_cap: int = 10
    billing_tier: str = "unknown"
    project_id: str | None = None
    credential_sha256: str | None = None
    verified_at: str | None = None
    verification_source: str | None = None
    requests_per_minute: int | None = None
    input_tokens_per_minute: int | None = None
    requests_per_day: int | None = None

    def __post_init__(self):
        if self.model != "gemini-3.1-flash-lite":
            raise ValueError("S02 free-only policy permits only the verified Flash-Lite model.")
        if self.billing_tier not in {"unknown", "free", "paid"}:
            raise ValueError("Invalid billing tier.")
        if type(self.daily_analysis_cap) is not int or self.daily_analysis_cap < 0:
            raise ValueError("Daily analysis cap must be a nonnegative integer.")
        for field in ("requests_per_minute", "input_tokens_per_minute", "requests_per_day"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Quota limits must be nonnegative integers or unknown.")

    def verify(self, key, model, instant):
        if self.billing_tier == "paid":
            stop("free_tier_required", "Paid Gemini project blocked; there is no paid-tier fallback.")
        if self.billing_tier != "free" or model != self.model:
            stop("free_tier_unverified", "Confirm this key's Free Tier project and model in Google AI Studio before provider calls.")
        if not self.project_id or not re.fullmatch(r"[a-zA-Z0-9_-]+", self.project_id):
            stop("free_tier_unverified", "The confirmed key-owning project is required.")
        if self.credential_sha256 != hashlib.sha256(key.encode()).hexdigest():
            stop("free_tier_unverified", "Free-tier confirmation is missing or belongs to another credential.")
        if self.verification_source != "google_ai_studio":
            stop("free_tier_unverified", "Record the billing tier and active quotas from Google AI Studio.")
        try:
            checked = datetime.fromisoformat(self.verified_at or "")
            if checked.tzinfo is None or not timedelta(0) <= instant - checked <= timedelta(hours=24):
                raise ValueError
        except (TypeError, ValueError):
            stop("free_tier_unverified", "Free-tier confirmation must be timezone-aware and no older than 24 hours.")
        if any(getattr(self, f) is None for f in
               ("requests_per_minute", "input_tokens_per_minute", "requests_per_day")):
            stop("free_tier_unverified", "Active RPM, input TPM and RPD quotas are unknown; provider calls blocked.")


class AnalysisBudget:
    """Shared project/model ledger across runs/output folders; reserve before work."""
    def __init__(self, policy, key, model, root="output/gemini-analysis-usage", *, clock=time.time):
        self.policy, self.key, self.model, self.clock = policy, key, model, clock
        self.store = AcquisitionStore(root)
        identity = f"{policy.project_id}:{model}"
        self.relative = hashlib.sha256(identity.encode()).hexdigest() + ".json"

    def verify(self):
        self.policy.verify(self.key, self.model, datetime.fromtimestamp(self.clock(), timezone.utc))

    @contextmanager
    def state(self):
        self.verify()
        lock = self.store.path(self.relative + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            instant = self.clock()
            day = datetime.fromtimestamp(instant, ZoneInfo("America/Los_Angeles")).date().isoformat()
            value = self.store.read(self.relative)
            if not value or value["day"] != day:
                recent = value["recent_requests"] if value else []
                value = {"day": day, "analyses": 0, "requests": 0, "recent_requests": recent, "blocked": False}
            value["recent_requests"] = [r for r in value["recent_requests"] if r["at"] > instant - 60]
            yield value, instant
            self.store.write(self.relative, value)

    def available(self, state):
        if state["blocked"]:
            stop("quota_exhausted", "Provider reported quota exhaustion; paused until the next Pacific quota day.")
        if state["requests"] >= self.policy.requests_per_day:
            stop("quota_exhausted", "Configured daily request quota exhausted.")

    def start_analysis(self):
        with self.state() as (state, _):
            self.available(state)
            if state["analyses"] >= self.policy.daily_analysis_cap:
                stop("daily_analysis_cap", "Daily uncached analysis cap reached; no further media will be processed.")
            state["analyses"] += 1

    def reserve_request(self, input_tokens):
        if type(input_tokens) is not int or input_tokens < 0:
            stop("free_tier_unverified", "Input token count unavailable; generation blocked.")
        with self.state() as (state, instant):
            self.available(state)
            recent = state["recent_requests"]
            if len(recent) >= self.policy.requests_per_minute:
                stop("quota_exhausted", "Configured rolling-minute request quota reached; stop this run and retry later.")
            if sum(r["tokens"] for r in recent) + input_tokens > self.policy.input_tokens_per_minute:
                stop("quota_exhausted", "Configured input-token quota would be exceeded; no generation submitted.")
            state["requests"] += 1
            recent.append({"at": instant, "tokens": input_tokens})

    def provider_exhausted(self):
        # A generic 429 does not reliably identify RPM versus RPD: stop conservatively.
        with self.state() as (state, _):
            state["blocked"] = True


def load_policy(path):
    return FreeTierPolicy(**json.loads(Path(path).read_text()))


def main(argv=None):
    """Record an operator's actual console check; never claim an API billing check."""
    import argparse
    import os
    from dataclasses import asdict
    from app.services.acquisition_cli import load_local_env
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-free-tier", action="store_true", required=True,
                        help="Confirm you checked this key's owning project is Free Tier in Google AI Studio")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--rpm", required=True, type=int)
    parser.add_argument("--tpm", required=True, type=int)
    parser.add_argument("--rpd", required=True, type=int)
    parser.add_argument("--daily-cap", default=10, type=int)
    parser.add_argument("--output", default="output/gemini-free-tier.json")
    args = parser.parse_args(argv)
    load_local_env(".env")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        parser.error("GEMINI_API_KEY is required; no credentials are printed.")
    try:
        policy = FreeTierPolicy(billing_tier="free", project_id=args.project_id,
            credential_sha256=hashlib.sha256(key.encode()).hexdigest(),
            verified_at=datetime.now(timezone.utc).isoformat(), verification_source="google_ai_studio",
            requests_per_minute=args.rpm, input_tokens_per_minute=args.tpm, requests_per_day=args.rpd,
            daily_analysis_cap=args.daily_cap)
        policy.verify(key, policy.model, datetime.now(timezone.utc))
    except (ValueError, AcquisitionFailure):
        parser.error("Invalid project, quota or cap configuration.")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(policy), indent=2) + "\n")
    print(json.dumps({"status": "operator_confirmation_recorded", "provider_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
