"""Load and validate a brand's local relevance universe."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.relevance import RelevanceDomain, RelevanceLevel, RelevanceUniverse


class RelevanceConfigError(ValueError):
    """Invalid JSON, schema, or brand linkage in relevance configuration."""


def _fields(data: Any, required: set[str], optional: set[str], location: str) -> None:
    if not isinstance(data, dict):
        raise RelevanceConfigError(f"{location} must be an object.")
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing or unknown:
        raise RelevanceConfigError(
            f"{location}: missing fields {sorted(missing)}; unknown fields {sorted(unknown)}."
        )


def load_relevance_config(
    path: str | Path, *, expected_brand_id: str
) -> RelevanceUniverse:
    """Load JSON with explicit brand linkage; no inference or agent execution."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RelevanceConfigError(f"Invalid relevance JSON: {exc}") from exc

    _fields(data, {"brand_id", "domains"}, set(), "universe")
    if data["brand_id"] != expected_brand_id:
        raise RelevanceConfigError("Relevance universe brand_id does not match expected_brand_id.")
    if not isinstance(data["domains"], list):
        raise RelevanceConfigError("domains must be a list.")

    domains = []
    for index, item in enumerate(data["domains"]):
        _fields(
            item,
            {"id", "name", "relevance_level", "rationale", "transferable_signals", "selection_criteria"},
            {"restrictions", "named_entities", "geographic_restrictions"},
            f"domains[{index}]",
        )
        try:
            domain = RelevanceDomain(
                **{**item, "relevance_level": RelevanceLevel(item["relevance_level"])}
            )
        except (ValueError, TypeError) as exc:
            raise RelevanceConfigError(f"domains[{index}]: {exc}") from exc
        domains.append(domain)
    try:
        return RelevanceUniverse(brand_id=data["brand_id"], domains=domains)
    except ValueError as exc:
        raise RelevanceConfigError(str(exc)) from exc
