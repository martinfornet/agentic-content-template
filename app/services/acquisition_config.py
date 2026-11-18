"""Operational limits and discovery inputs, separate from editorial strategy."""
from dataclasses import dataclass, field
import json
from pathlib import Path

from app.models.brand_intelligence import BrandIntelligence


@dataclass(slots=True)
class AcquisitionConfig:
    brand_id: str
    queries: list[dict] = field(default_factory=list)
    seed_accounts: list[dict] = field(default_factory=list)
    max_accounts: int = 60
    queries_per_run: int = 2
    results_per_query: int = 20
    recent_per_account: int = 3
    new_accounts_per_day: int = 10
    media_per_day: int = 30
    media_per_account_per_day: int = 3
    adjacent_media_per_day: int = 6
    history_per_account: int = 20
    history_days: int = 60
    observation_days: int = 30
    retention_days: int = 30
    refresh_hours: list[int] = field(default_factory=lambda: [24, 72, 168])
    passes_per_day: int = 3
    metric_refreshes_per_run: int = 30
    max_media_bytes: int = 100_000_000
    http_timeout_seconds: int = 60
    retries: int = 2
    provider_poll_seconds: int = 5
    provider_wait_seconds: int = 180
    include_shares: bool = False
    relevance_model: str = "gemini-3.1-flash-lite"

    def __post_init__(self):
        for name in ("max_accounts", "queries_per_run", "results_per_query", "recent_per_account",
                     "new_accounts_per_day", "media_per_day", "media_per_account_per_day", "adjacent_media_per_day",
                     "metric_refreshes_per_run", "history_per_account", "history_days",
                     "observation_days", "retention_days", "passes_per_day", "max_media_bytes",
                     "http_timeout_seconds", "provider_poll_seconds", "provider_wait_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if type(self.retries) is not int or not 0 <= self.retries <= 5:
            raise ValueError("retries must be between zero and five.")
        if not isinstance(self.refresh_hours, list) or not self.refresh_hours or any(
            type(v) is not int or v <= 0 for v in self.refresh_hours
        ) or self.refresh_hours != sorted(set(self.refresh_hours)):
            raise ValueError("refresh_hours must be increasing positive hours.")
        if type(self.include_shares) is not bool:
            raise ValueError("include_shares must be boolean.")
        if not isinstance(self.queries, list) or not isinstance(self.seed_accounts, list):
            raise ValueError("queries and seed_accounts must be lists.")
        if not isinstance(self.relevance_model, str) or not self.relevance_model.strip():
            raise ValueError("relevance_model must be nonempty.")


def load_acquisition_config(path, context: BrandIntelligence) -> AcquisitionConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = data.pop("schema_version", None)
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported acquisition configuration schema.")
    provider = data.pop("provider", None)
    if provider != "apify_instagram":
        raise ValueError("Only apify_instagram is configured for V0.")
    config = AcquisitionConfig(**data)
    if config.brand_id != context.brand_id:
        raise ValueError("Acquisition brand must match Brand Intelligence.")
    for item in config.queries + config.seed_accounts:
        if not isinstance(item, dict):
            raise ValueError("Acquisition inputs must be objects.")
        if set(item) - {"term", "kind", "handle", "domain_id"}:
            raise ValueError("Unknown acquisition input fields.")
        domain = item["domain_id"]
        if context.is_domain_excluded(domain):
            raise ValueError("Excluded domains cannot be acquisition targets.")
        key = "term" if "term" in item else "handle"
        if not isinstance(item[key], str) or not item[key].strip():
            raise ValueError("Acquisition input must be nonempty.")
        if "term" in item and item.get("kind", "content_search") not in {"content_search", "account_search"}:
            raise ValueError("Invalid search kind.")
    return config
