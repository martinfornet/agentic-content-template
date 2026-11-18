"""Small replaceable provider boundary; raw data and coverage survive normalization."""
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app.models.acquisition import DiscoveryRequest


@dataclass(slots=True)
class ProviderResult:
    items: list[dict[str, Any]]
    raw: Any
    retrieved_at: str
    job_id: str | None = None
    measured_at: str | None = None
    cursor: str | None = None
    limitations: list[str] = field(default_factory=list)
    cost_usd: float | None = None


class InstagramProvider(Protocol):
    name: str
    def discover(self, request: DiscoveryRequest, *, job_id=None, on_job: Callable | None = None) -> ProviderResult: ...
    def get_account(self, reference: str, *, job_id=None, on_job: Callable | None = None) -> ProviderResult: ...
    def list_account_content(self, reference: str, *, since: str, limit: int, cursor=None,
                             job_id=None, on_job: Callable | None = None) -> ProviderResult: ...
    def get_content(self, reference: str, *, job_id=None, on_job: Callable | None = None) -> ProviderResult: ...
