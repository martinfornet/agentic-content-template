"""Small public-document parsers; extraction is evidence, not semantic acceptance."""
import hashlib
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

SOCIAL_HOSTS = {'instagram.com', 'www.instagram.com', 'youtube.com', 'www.youtube.com',
                'youtu.be', 'tiktok.com', 'www.tiktok.com', 'linkedin.com', 'www.linkedin.com',
                'fr.linkedin.com', 'facebook.com', 'www.facebook.com', 'twitter.com', 'x.com'}


def canonical_url(url, base=None):
    parsed = urlsplit(urljoin(base or '', url))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Public HTTPS URL required.')
    if parsed.port not in (None, 443):
        raise ValueError('Nonstandard URL port rejected.')
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    host, path = parsed.hostname.lower(), parsed.path or '/'
    if host in {'instagram.com', 'www.instagram.com'}:
        host = 'www.instagram.com'
        match = re.fullmatch(r'/(?:p|reel|reels)/([^/]+)/?', path)
        if match:
            path = '/p/' + match.group(1) + '/'
    return urlunsplit(('https', host, path, urlencode(sorted(query)), ''))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class PageParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.title, self.text, self.links, self.json_ld = base, [], [], [], []
        self.skip = 0
        self.in_title = False
        self.json_script = False
        self.script_parts = []
        self.anchor = None
        self.restricted = False
        self.navigation_depth = 0

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        if tag in {"nav", "footer"}:
            self.navigation_depth += 1
        if tag == 'meta' and a.get('name', '').lower() in {'robots', 'googlebot'}:
            if 'noindex' in a.get('content', '').lower():
                self.restricted = True
        if tag in {'script', 'style', 'noscript'}:
            self.skip += 1
            self.json_script = tag == 'script' and a.get('type', '').lower() == 'application/ld+json'
            self.script_parts = []
        if tag == 'img' and self.anchor and not self.skip:
            alt = next((value for name, value in attrs if name == 'alt' and value and value.strip()), '')
            if alt:
                self.anchor[1].append(alt)
                self.anchor[2]['image_label'] = True
        if tag == 'title':
            self.in_title = True
        if tag in {'p', 'div', 'h1', 'h2', 'h3', 'li', 'br', 'article', 'section'} and not self.skip:
            self.text.append('\n')
        if tag == 'a' and a.get('href') and not self.skip:
            try:
                url = canonical_url(a['href'], self.base)
            except ValueError:
                return
            self.anchor = [url, [], {'navigation': bool(self.navigation_depth),
                                     'nofollow': 'nofollow' in a.get('rel', '').lower().split()}]
        if tag == 'link' and a.get('href') and a.get('type') in {'application/rss+xml', 'application/atom+xml'}:
            try:
                self.links.append({'url': canonical_url(a['href'], self.base), 'label': 'RSS/Atom feed', 'relation': 'feed'})
            except ValueError:
                pass

    def handle_endtag(self, tag):
        if tag in {"nav", "footer"}:
            self.navigation_depth = max(0, self.navigation_depth - 1)
        if tag == 'a' and self.anchor:
            self.links.append({'url': self.anchor[0], 'label': ' '.join(self.anchor[1]).strip(), 'relation': 'linked_mention', **self.anchor[2]})
            self.anchor = None
        if tag == 'title':
            self.in_title = False
        if tag in {'script', 'style', 'noscript'}:
            if self.json_script:
                try:
                    self.json_ld.append(json.loads(''.join(self.script_parts)))
                except ValueError:
                    pass
            self.json_script = False
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if self.json_script:
            self.script_parts.append(data)
        if not self.skip:
            self.text.append(data)
            if self.in_title:
                self.title.append(data)
            if self.anchor:
                self.anchor[1].append(data)


def plain_html(text):
    p = PageParser('https://unused.example/')
    p.feed(text)
    return re.sub(r'\s+', ' ', ' '.join(p.text)).strip()


def structured_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from structured_nodes(item)
    elif isinstance(value, dict):
        yield value
        # Traverse nested entities, not just top-level graphs.
        for item in value.values():
            if isinstance(item, (list, dict)):
                yield from structured_nodes(item)


def parse_document(raw, url, mime='text/html'):
    """Return text, links and attributed candidate records without fetching anything."""
    if mime != 'application/xhtml+xml' and ('xml' in mime or raw.lstrip().startswith(('<?xml', '<rss', '<feed'))):
        if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
            raise ValueError('XML declarations not supported.')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError('Malformed public feed XML.') from exc
        links, candidates, parts = [], [], []
        for item in root.iter():
            if item.tag.split('}')[-1] not in {'item', 'entry'}:
                continue
            values = {}
            for child in item:
                tag = child.tag.split('}')[-1]
                if tag == 'link':
                    if child.attrib.get('rel', 'alternate') == 'alternate':
                        values['link'] = child.attrib.get('href') or child.text
                elif tag in {'title', 'description', 'summary', 'content', 'pubDate', 'published', 'updated'}:
                    values[tag] = ''.join(child.itertext())
            title = plain_html(values.get('title', ''))
            text = plain_html(values.get('description') or values.get('summary') or values.get('content') or '')
            target = None
            if values.get('link'):
                try:
                    target = canonical_url(values['link'].strip(), url)
                    links.append({'url': target, 'label': title, 'relation': 'feed_entry'})
                except ValueError:
                    pass
            parts.append(title + '\n' + text)
            if title:
                candidates.append({'title': title, 'text': text, 'identity_url': target,
                                   'subject_type': 'publication', 'published_at': values.get('pubDate') or values.get('published'),
                                   'location': None, 'start_date': None, 'end_date': None})
        return {'title': 'Public feed', 'text': '\n'.join(parts), 'links': links,
                'candidates': candidates, 'structured': [], 'restricted': False}
    p = PageParser(url)
    p.feed(raw)
    text = re.sub(r'[ \t\r\f\v]+', ' ', ''.join(p.text))
    candidates = []
    for node in structured_nodes(p.json_ld):
        kinds = node.get('@type', [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not isinstance(kinds, list):
            continue
        if not set(kinds) & {'Event', 'Place', 'Restaurant', 'Hotel', 'Museum', 'TouristAttraction',
                              'ExhibitionEvent', 'Article', 'NewsArticle', 'BlogPosting'}:
            continue
        title = node.get('name') or node.get('headline')
        if not isinstance(title, str) or not title.strip():
            continue
        target = node.get('url') or node.get('@id')
        try:
            target = canonical_url(target, url) if isinstance(target, str) else None
        except ValueError:
            target = None
        candidates.append({'title': title, 'text': plain_html(str(node.get('description', ''))),
                           'identity_url': target, 'subject_type': kinds[0], 'location': node.get('location') or node.get('address'),
                           'start_date': node.get('startDate'), 'end_date': node.get('endDate'),
                           'published_at': node.get('datePublished')})
    return {'title': ' '.join(p.title).strip() or url, 'text': text.strip(), 'links': p.links,
            'candidates': candidates, 'structured': p.json_ld, 'restricted': p.restricted}
