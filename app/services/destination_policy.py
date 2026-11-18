"""Require explicit allow-listing for remote worker destinations."""
from urllib.parse import urlsplit

def validate_origin(origin, allowed_hosts):
    parsed = urlsplit(origin or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {str(value).lower().rstrip(".") for value in (allowed_hosts or [])}
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("Invalid worker origin.")
    if not allowed or host not in allowed:
        raise ValueError("Worker origin is not explicitly allow-listed.")
    return parsed
