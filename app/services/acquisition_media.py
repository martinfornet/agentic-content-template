"""Bounded reference-video downloads and technical probing, not video analysis."""
from datetime import datetime, UTC, timedelta
import hashlib
import ipaddress
import json
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.models.acquisition import AcquisitionError, AcquisitionFailure, MediaState, now


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AcquisitionFailure(AcquisitionError("invalid_media_url", "Media requires a public HTTPS URL."))
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError:
        raise AcquisitionFailure(AcquisitionError("network_failure", "Media host could not be resolved.", True))
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise AcquisitionFailure(AcquisitionError("invalid_media_url", "Non-public media host rejected."))


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_video(path):
    try:
        output = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                capture_output=True, check=True, timeout=30)
        data = json.loads(output.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise AcquisitionFailure(AcquisitionError("invalid_media", "Downloaded bytes are not readable media."))
    if not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
        raise AcquisitionFailure(AcquisitionError("invalid_media", "Downloaded file has no video stream."))
    return {"has_audio": any(s.get("codec_type") == "audio" for s in data["streams"])}


class MediaDownloader:
    def __init__(self, store, *, max_bytes=100_000_000, timeout=60, opener=None, probe=probe_video, validate_url=public_url):
        self.store, self.max_bytes, self.timeout = store, max_bytes, timeout
        self.opener, self.probe, self.validate_url = opener or build_opener(CheckedRedirect()), probe, validate_url
        self.request_count = 0

    def acquire(self, media):
        if media.state is MediaState.LOCAL_READY and media.local_path and self.store.path(media.local_path).is_file():
            return
        if not media.remote_url:
            raise AcquisitionFailure(AcquisitionError("media_unavailable", "Provider supplied no video URL."))
        if media.expires_at and datetime.fromisoformat(media.expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
            media.state = MediaState.EXPIRED
            raise AcquisitionFailure(AcquisitionError("media_expired", "Media URL has expired; refresh content metadata."))
        self.validate_url(media.remote_url)
        relative = f"observations/{media.observation_id}/media/{media.id}.mp4"
        target = self.store.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        digest, total = hashlib.sha256(), 0
        started = time.monotonic()
        try:
            self.request_count += 1
            with self.opener.open(Request(media.remote_url), timeout=self.timeout) as response, temporary.open("wb") as out:
                while block := response.read(64 * 1024):
                    if time.monotonic() - started > self.timeout:
                        raise AcquisitionFailure(AcquisitionError("network_failure", "Media transfer exceeded configured deadline.", True))
                    total += len(block)
                    if total > self.max_bytes:
                        raise AcquisitionFailure(AcquisitionError("media_too_large", "Media exceeded configured byte limit."))
                    out.write(block)
                    digest.update(block)
                mime = response.headers.get_content_type()
            technical = self.probe(temporary)
            temporary.replace(target)
        except HTTPError as exc:
            raise AcquisitionFailure(AcquisitionError("media_unavailable", f"Media HTTP request failed ({exc.code}).",
                                                     exc.code == 429 or exc.code >= 500, exc.code))
        except (URLError, OSError, TimeoutError):
            raise AcquisitionFailure(AcquisitionError("network_failure", "Media retrieval failed.", True))
        finally:
            temporary.unlink(missing_ok=True)
        media.local_path, media.checksum, media.size_bytes = relative, digest.hexdigest(), total
        media.mime_type, media.has_audio = mime, technical["has_audio"]
        media.state, media.retrieved_at, media.removed_at = MediaState.LOCAL_READY, now(), None

    def cleanup(self, retention_days, *, at=None):
        cutoff = (at or datetime.now(UTC)) - timedelta(days=retention_days)
        removed = []
        for content in self.store.contents():
            for media in content.media:
                if (not media.protected and media.state is MediaState.LOCAL_READY and media.retrieved_at
                        and datetime.fromisoformat(media.retrieved_at) < cutoff):
                    self.store.path(media.local_path).unlink(missing_ok=True)
                    media.state, media.removed_at = MediaState.REMOVED, now()
                    removed.append(media.id)
            self.store.save_content(content)
        return removed
