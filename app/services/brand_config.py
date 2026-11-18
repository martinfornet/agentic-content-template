"""Load local brand configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models import Brand


BRAND_CONFIG_FIELDS = {
    "id",
    "name",
    "mission",
    "positioning",
    "audience",
    "tone",
    "editorial_rules",
    "prohibited_topics_or_claims",
    "enabled_content_formats",
    "voice_configuration",
}

REQUIRED_BRAND_CONFIG_FIELDS = BRAND_CONFIG_FIELDS


class BrandConfigError(ValueError):
    """Raised when a brand configuration cannot be loaded safely."""


def load_brand_config(path: str | Path) -> Brand:
    """Load a local brand.yaml file into a Brand model.

    This parser intentionally supports only the small YAML subset used by our
    template: top-level scalars, top-level lists, and one-level nested mappings.
    """

    config_path = Path(path)
    data = _parse_restricted_yaml(config_path.read_text(encoding="utf-8"))
    _validate_brand_config(data)
    return Brand(
        id=data["id"],
        name=data["name"],
        mission=data["mission"],
        positioning=data["positioning"],
        audience=data["audience"],
        tone=data["tone"],
        editorial_rules=data["editorial_rules"],
        prohibited_topics_or_claims=data["prohibited_topics_or_claims"],
        enabled_content_formats=data["enabled_content_formats"],
        voice_configuration=data["voice_configuration"],
    )


def _validate_brand_config(data: dict[str, Any]) -> None:
    missing = REQUIRED_BRAND_CONFIG_FIELDS - data.keys()
    if missing:
        raise BrandConfigError(f"Missing brand config fields: {sorted(missing)}")

    unknown = data.keys() - BRAND_CONFIG_FIELDS
    if unknown:
        raise BrandConfigError(f"Unknown brand config fields: {sorted(unknown)}")

    if not isinstance(data["id"], str) or not data["id"]:
        raise BrandConfigError("Brand id must be a non-empty string.")

    if not isinstance(data["name"], str) or not data["name"]:
        raise BrandConfigError("Brand name must be a non-empty string.")

    for field_name in ("mission", "positioning", "audience", "tone"):
        value = data[field_name]
        if value is not None and not isinstance(value, str):
            raise BrandConfigError(f"{field_name} must be a string or null.")

    for field_name in (
        "editorial_rules",
        "prohibited_topics_or_claims",
        "enabled_content_formats",
    ):
        value = data[field_name]
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise BrandConfigError(f"{field_name} must be a list of strings.")

    if not isinstance(data["voice_configuration"], dict):
        raise BrandConfigError("voice_configuration must be a mapping.")


def _parse_restricted_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        if not raw_line.startswith(" "):
            key, value = _split_key_value(line)
            if value == "":
                data[key] = {}
                current_key = key
            else:
                data[key] = _parse_scalar(value)
                current_key = None
            continue

        if current_key is None:
            raise BrandConfigError(f"Unexpected indented line: {raw_line}")

        stripped = line.strip()
        if stripped.startswith("- "):
            if not isinstance(data[current_key], list):
                data[current_key] = []
            data[current_key].append(_parse_scalar(stripped[2:]))
            continue

        nested_key, nested_value = _split_key_value(stripped)
        if not isinstance(data[current_key], dict):
            raise BrandConfigError(f"Cannot add mapping item under {current_key}.")
        data[current_key][nested_key] = _parse_scalar(nested_value)

    return data


def _split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise BrandConfigError(f"Expected key/value line: {line}")
    key, value = line.split(":", 1)
    key = key.strip()
    if not key:
        raise BrandConfigError(f"Missing key in line: {line}")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    if value in {"null", "~"}:
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
