"""Load local subject datasets without research or enrichment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.source import Source, SourceType
from app.models.subject import Assertion, SubjectDataset, SubjectRecord, VerificationStatus


class DatasetError(ValueError):
    """Invalid dataset JSON, schema, or references."""


def _fields(data: Any, required: set[str], optional: set[str]) -> None:
    if not isinstance(data, dict):
        raise DatasetError("Dataset records must be objects.")
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing or unknown:
        raise DatasetError(f"Missing fields {sorted(missing)}; unknown fields {sorted(unknown)}.")


def _assertion(data: Any) -> Assertion:
    _fields(data, {"id", "subject_id", "field", "value", "source_references", "verification_status"},
            {"uncertainty_note"})
    return Assertion(**{**data, "verification_status": VerificationStatus(data["verification_status"])})


def load_subject_dataset(path: str | Path, *, expected_brand_id: str) -> SubjectDataset:
    """Load any record count; only the controlled V0 fixture requires ten places."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        _fields(data, {"schema_version", "id", "brand_id", "selection_purpose", "sources", "subjects"}, set())
        if data["brand_id"] != expected_brand_id:
            raise DatasetError("Dataset brand_id does not match expected_brand_id.")
        if not isinstance(data["sources"], list) or not isinstance(data["subjects"], list):
            raise DatasetError("sources and subjects must be lists.")
        sources = []
        for item in data["sources"]:
            _fields(item, {"id", "brand_id", "type", "title", "url_or_path"},
                    {"raw_content", "extracted_content", "metadata", "created_at", "updated_at"})
            for name in ("id", "brand_id", "title", "url_or_path"):
                if not isinstance(item[name], str) or not item[name].strip():
                    raise DatasetError(f"Source {name} must be a non-empty string.")
            for name in ("raw_content", "extracted_content", "created_at", "updated_at"):
                if name in item and not isinstance(item[name], str):
                    raise DatasetError(f"Source {name} must be a string.")
            if "metadata" in item and not isinstance(item["metadata"], dict):
                raise DatasetError("Source metadata must be an object.")
            sources.append(Source(**{**item, "type": SourceType(item["type"])}))
        subjects = []
        for item in data["subjects"]:
            _fields(item, {"id", "subject_type", "seed_data", "assertions"}, set())
            if not isinstance(item["seed_data"], dict) or not isinstance(item["assertions"], list):
                raise DatasetError("seed_data must be an object and assertions must be a list.")
            subjects.append(SubjectRecord(
                id=item["id"], subject_type=item["subject_type"],
                seed_data={name: _assertion(value) for name, value in item["seed_data"].items()},
                assertions=[_assertion(value) for value in item["assertions"]],
            ))
        return SubjectDataset(**{**data, "sources": sources, "subjects": subjects})
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DatasetError(f"Invalid subject dataset: {exc}") from exc
