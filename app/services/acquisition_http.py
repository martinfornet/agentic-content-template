"""Standard-library JSON transport with bounded, sanitized failures."""
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.models.acquisition import AcquisitionError, AcquisitionFailure


class JsonHttp:
    def __init__(self, timeout=60, retries=2, sleep=time.sleep):
        self.timeout, self.retries, self.sleep = timeout, retries, sleep

    def request(self, method, url, *, headers=None, body=None):
        # Retry reads only. A lost POST response may already represent a paid job.
        retries = self.retries if method == "GET" else 0
        for attempt in range(retries + 1):
            try:
                request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                                  headers={"Content-Type": "application/json", **(headers or {})}, method=method)
                with urlopen(request, timeout=self.timeout) as response:
                    return json.load(response, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))
            except HTTPError as exc:
                status = exc.code
                code = ("authentication_failure" if status in (401, 403) else
                        "payment_required" if status == 402 else "rate_limit" if status == 429 else
                        "content_unavailable" if status == 404 else "provider_failure")
                try:
                    delay = float(exc.headers.get("Retry-After", 0)) or None
                except (ValueError, TypeError):
                    delay = None
                error = AcquisitionError(code, f"HTTP request failed ({status}).", status == 429 or status >= 500,
                                         status, delay)
            except (URLError, TimeoutError, socket.timeout, OSError):
                error = AcquisitionError("network_failure", "Network request failed; response unavailable.", True)
            except (ValueError, UnicodeError):
                raise AcquisitionFailure(AcquisitionError("invalid_response", "Response was not valid JSON."))
            if not error.retryable or attempt >= retries:
                raise AcquisitionFailure(error)
            self.sleep(min(error.retry_after_seconds or 2 ** attempt, 30))
