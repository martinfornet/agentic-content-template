"""Bounded public HTTPS collection with robots admission; no paid-provider client."""
import time
import gzip
import io
from urllib.error import HTTPError
from urllib.parse import urlsplit, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from app.models.acquisition import AcquisitionFailure
from app.services.acquisition_media import public_url
from app.services.ecosystem_parse import canonical_url

USER_AGENT = 'AgenticContentEcosystem/0.1'


class AccessFailure(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicWeb:
    def __init__(self, max_requests=30, max_bytes=2_000_000, timeout=20, sleep=time.sleep):
        self.max_requests, self.max_bytes, self.timeout = max_requests, max_bytes, timeout
        self.requests = 0
        self.opener = build_opener(NoRedirect())
        self.robots, self.last_request, self.delays = {}, {}, {}
        self.robots_failures = {}
        self.sleep = sleep

    def _read(self, url):
        try:
            public_url(url)  # Reuse acquisition's public-host guard.
        except AcquisitionFailure as exc:
            raise AccessFailure('public_url_unavailable:' + str(exc)) from exc
        if self.requests >= self.max_requests:
            raise AccessFailure('request_limit')
        host = urlsplit(url).netloc
        pause = max(1.0, self.delays.get(host, 1.0))
        remaining = pause - (time.monotonic() - self.last_request.get(host, 0))
        if remaining > 0:
            self.sleep(remaining)
        self.requests += 1
        self.last_request[host] = time.monotonic()
        try:
            with self.opener.open(Request(url, headers={'User-Agent': USER_AGENT,
                                                       'Accept': 'text/html,application/rss+xml,application/atom+xml,application/xml'}),
                                  timeout=self.timeout) as response:
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise AccessFailure('response_too_large')
                encoding = response.headers.get('Content-Encoding', 'identity').strip().lower()
                if encoding == 'gzip':
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                            data = compressed.read(self.max_bytes + 1)
                    except (OSError, EOFError) as exc:
                        raise AccessFailure('invalid_gzip_response') from exc
                    if len(data) > self.max_bytes:
                        raise AccessFailure('response_too_large')
                elif encoding != 'identity':
                    raise AccessFailure('unsupported_content_encoding')
                mime = response.headers.get_content_type()
                if mime not in {'text/html', 'application/xhtml+xml', 'text/plain', 'application/xml',
                                'text/xml', 'application/rss+xml', 'application/atom+xml'}:
                    raise AccessFailure('unsupported_content_type:' + mime)
                charset = response.headers.get_content_charset() or 'utf-8'
                return {'url': url, 'raw': data.decode(charset, errors='replace'), 'mime': mime}
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                raise AccessFailure('redirect:' + urljoin(url, exc.headers.get('Location', '')))
            raise AccessFailure('http_' + str(exc.code)) from exc

    def _admit(self, url):
        parts = urlsplit(url)
        host = parts.netloc
        if host in self.robots_failures:
            raise AccessFailure(self.robots_failures[host])
        if host not in self.robots:
            robots_url = 'https://' + host + '/robots.txt'
            try:
                raw = self._read(robots_url)['raw']
            except AccessFailure as exc:
                if str(exc) == 'http_404':
                    raw = ''
                else:
                    # Unknown robots access is not permission to collect.
                    reason = 'robots_unavailable:' + str(exc)
                    self.robots_failures[host] = reason
                    raise AccessFailure(reason) from exc
            parser = RobotFileParser(robots_url)
            parser.parse(raw.splitlines())
            self.robots[host] = parser
            delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 1
            rate = parser.request_rate(USER_AGENT) or parser.request_rate('*')
            if rate:
                delay = max(delay, rate.seconds / rate.requests)
            self.delays[host] = delay
        if not self.robots[host].can_fetch(USER_AGENT, url):
            raise AccessFailure('robots_disallowed')
        if self.delays.get(host, 1) > 30:
            raise AccessFailure('crawl_delay_exceeds_run_limit')

    def fetch(self, url):
        url = canonical_url(url)
        for _ in range(5):
            self._admit(url)
            try:
                return self._read(url)
            except AccessFailure as exc:
                if not str(exc).startswith('redirect:'):
                    raise
                url = canonical_url(str(exc).removeprefix('redirect:'))
        raise AccessFailure('too_many_redirects')
