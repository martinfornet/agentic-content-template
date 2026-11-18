"""Credential-safe JSON artifacts shared by analysis and its provider adapter."""
import json
import os
from urllib.parse import quote


def redact_text(encoded):
    for name in ("GEMINI_API_KEY", "APIFY_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            for variant in (secret, quote(secret, safe="")):
                encoded = encoded.replace(variant, "[REDACTED]")
    return encoded


def write_safe(store, path, value, *, immutable=False):
    encoded = redact_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    store.write(path, json.loads(encoded), immutable=immutable)
