"""Bounded public photo/video sourcing, provenance retained; no generic filler."""
from datetime import UTC, datetime
from html.parser import HTMLParser
import json
import re
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from app.models.source import Source, SourceType
from app.services.visual_quality import native_quality, source_identity


def candidate_quality(width, height, config, video=False):
    """Keep originals useful as horizontal proposals, without claiming FHD crops."""
    quality = native_quality(width, height, config, {} if video else None)
    if not quality['passed'] and not video and config.get('visual_sourcing', {}).get('landscape_proposals'):
        from app.services.visual_presentation import landscape_quality
        landscape = landscape_quality(width, height, config)
        if landscape['passed']:
            return landscape
    if not quality['passed'] and not video and config.get('visual_sourcing', {}).get('full_photo_proposals'):
        from app.services.visual_presentation import full_photo_quality
        complete = full_photo_quality(width, height, config)
        if complete['passed']:
            return complete
    return {**quality, 'presentation': 'cover'}
from app.services.acquisition_media import public_url
from app.services.ecosystem_http import PublicWeb, AccessFailure, USER_AGENT
from app.services.ecosystem_parse import PageParser, canonical_url
from app.services.reel_pipeline import digest, file_hash


class ImageLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.images = base, []
        self.video_label = None
        self.image_group = 0
        self.schema_text = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'script' and a.get('type') == 'application/ld+json':
            self.schema_text = ''
        if a.get("data-item"):
            try:
                item = json.loads(a["data-item"])
                photo = item.get("image", {})
                variants = [s for group in photo.get("segmentedSources", {}).values() for s in group if s.get("url")]
                if variants:
                    largest = max(variants, key=lambda s: s.get("width", 0) * s.get("height", 0))
                    self.images.append({"url": canonical_url(largest["url"], self.base),
                                        "label": item.get("dangerousHed", "") + " — " + photo.get("altText", ""),
                                        "credit": photo.get("credit", ""), "kind": "structured_gallery", "caption": photo.get("caption", "")})
            except (ValueError, TypeError, AttributeError):
                pass
        urls = []
        ranks = {}
        if tag == "img":
            self.image_group += 1
            if any(token in (a.get("src") or "").lower() for token in ("/icon_", "logo", "pixel.gif", "/metro.", "/drapeau/", ".svg")):
                return
            urls = [a.get(k) for k in ("src", "data-src", "data-original", "data-guid", "data-lazy-src") if a.get(k)]
            for url in urls:
                sized = re.search(r'(\d+)x(\d+)(?=\.)', urlsplit(url).path)
                ranks[url] = int(sized[1]) if sized else 1_000_000
            for key in ("srcset", "data-srcset"):
                # CDN URLs themselves contain commas. Split only after a descriptor.
                for url, size, unit in re.findall(r'(\S+?)\s+(\d+(?:\.\d+)?)([wx])(?:\s*,|\s*$)', a.get(key) or ""):
                    urls.append(url)
                    density_width = float(a['width']) if (a.get('width') or '').isdigit() else 1000
                    ranks[url] = max(ranks.get(url, 0), float(size) * (density_width if unit == 'x' else 1))
            for key in ('data-original', 'data-guid'):
                if a.get(key):
                    ranks[a[key]] = 2_000_000
        if tag == "video":
            self.video_label = a.get("aria-label") or a.get("title") or ""
        if tag in {"video", "source"} and (tag == "video" or self.video_label is not None):
            if a.get("src"):
                self.images.append({"url": canonical_url(a["src"], self.base), "label": self.video_label or "", "kind": "video", "media_type": "VIDEO"})
        if tag == "meta" and (a.get("property") or a.get("name")) in {"og:video", "og:video:url", "og:video:secure_url"} and a.get("content"):
            self.images.append({"url": canonical_url(a["content"], self.base), "label": "", "kind": "video", "media_type": "VIDEO"})
        if tag == "meta" and (a.get("property") or a.get("name")) in {"og:image", "twitter:image"}:
            urls = [a.get("content")]
        for url in urls:
            if not url or url.startswith("data:"):
                continue
            try:
                normalized = canonical_url(url, self.base)
            except ValueError:
                continue
            self.images.append({"url": normalized, "label": a.get("alt") or a.get("title") or "", "kind": tag,
                                "variant_group": self.image_group if tag == 'img' else None, "variant_rank": ranks.get(url, 0)})

    def handle_endtag(self, tag):
        if tag == "video":
            self.video_label = None
        if tag == 'script' and self.schema_text is not None:
            try:
                value = json.loads(self.schema_text)
                nodes = value if isinstance(value, list) else [value]
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    if isinstance(node.get('@graph'), list):
                        nodes.extend(node['@graph'])
                    pictures = node.get('image', [])
                    for picture in pictures if isinstance(pictures, list) else [pictures]:
                        url = picture if isinstance(picture, str) else (picture.get('contentUrl') or picture.get('url')) if isinstance(picture, dict) else None
                        if url:
                            self.images.append({'url': canonical_url(url, self.base), 'label': str(node.get('name') or node.get('headline') or ''), 'kind': 'structured_gallery'})
            except (ValueError, TypeError):
                pass
            self.schema_text = None

    def handle_data(self, data):
        if self.schema_text is not None:
            self.schema_text += data


class PublicMediaWeb(PublicWeb):
    def fetch_image(self, url):
        return self.fetch_media(url, video=False)

    def fetch_video(self, url):
        return self.fetch_media(url, video=True)

    def fetch_media(self, url, *, video):
        canonical_url(url)  # Validate; preserve CDN query order when following its redirects.
        url = url.split("#", 1)[0]
        for _ in range(5):
            self._admit(url)
            public_url(url)
            if self.requests >= self.max_requests:
                raise AccessFailure("request_limit")
            host = urlsplit(url).netloc
            remaining = max(1, self.delays.get(host, 1)) - (time.monotonic() - self.last_request.get(host, 0))
            if remaining > 0:
                self.sleep(remaining)
            self.last_request[host] = time.monotonic()
            self.requests += 1
            try:
                allowed = {"video/mp4", "video/quicktime"} if video else {"image/jpeg", "image/png", "image/webp"}
                limit = (100 if video else 25) * 1024 * 1024
                with self.opener.open(Request(url, headers={"User-Agent": USER_AGENT, "Accept": ",".join(sorted(allowed))}), timeout=self.timeout) as response:
                    mime = response.headers.get_content_type()
                    if mime not in allowed:
                        raise AccessFailure("unsupported_media_type:" + mime)
                    data = response.read(limit + 1)
                    if len(data) > limit:
                        raise AccessFailure("media_too_large")
                    return {"url": url, "mime": mime, "data": data}
            except HTTPError as error:
                if error.code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, error.headers.get("Location", ""))
                    canonical_url(url)
                    continue
                raise AccessFailure("http_" + str(error.code)) from error
        raise AccessFailure("too_many_redirects")


def preferred_links(links, name):
    """Try each observed original/largest variant once, not its thumbnail family."""
    groups = {}
    for link in links:
        key = ('img', link['variant_group']) if link.get('variant_group') is not None else ('url', link['url'])
        if key not in groups or link.get('variant_rank', 0) > groups[key].get('variant_rank', 0):
            groups[key] = link
    # A place name in the observed filename is evidence to inspect, not approval.
    return sorted(groups.values(), key=lambda x: name.casefold() not in (x['label']+' '+urlsplit(x['url']).path).casefold())


def balanced_media_links(links):
    # Interleave photos and videos so a page budget cannot hide either medium.
    photos = [link for link in links if link.get('media_type') != 'VIDEO']
    videos = [link for link in links if link.get('media_type') == 'VIDEO']
    result = []
    for index in range(max(len(photos), len(videos))):
        if index < len(photos):
            result.append(photos[index])
        if index < len(videos):
            result.append(videos[index])
    return result


def research(pipeline, choice_id, pages, *, client=None, max_images=12, image_results=None):
    dossier, _ = pipeline.dossier(choice_id)
    name = dossier["place"]["name"]
    web = client or PublicMediaWeb(max_requests=45, max_bytes=4_000_000)
    run = {"id": digest({"at": datetime.now(UTC).isoformat(), "choice": choice_id})[:32],
           "place_id": dossier["place"]["id"], "pages": [], "candidates": [], "failures": [], "http_requests": 0,
           "paid_calls": 0, "deferred": [], "notice": "Recherche bornée assistant, non exhaustive ; aucune validation éditoriale/factuelle propriétaire."}
    seen = set()
    pages = list(dict.fromkeys(pages))
    for page_index, url in enumerate(pages):
        remaining = getattr(web, 'max_requests', 45) - web.requests
        if remaining <= 0:
            run['deferred'].append({'url': url, 'reason': 'request_budget', 'page_not_explored': True})
            continue
        # Reserve an equitable share for every supplied source, including robots/page requests.
        page_limit = web.requests + max(2, remaining // (len(pages)-page_index))
        try:
            page = web.fetch(url)
            parser = PageParser(page["url"])
            parser.feed(page["raw"])
            if parser.restricted:
                raise AccessFailure("page_restricted")
            source_id = "visual-page-" + digest({"url": page["url"], "raw": page["raw"]})[:24]
            source = Source(id=source_id, brand_id=pipeline.editorial.read()["brand_id"], type=SourceType.URL,
                            title=" ".join(parser.title), url_or_path=page["url"], raw_content=page["raw"], extracted_content=" ".join(parser.text),
                            metadata={"origin": "assistant_visual_research", "place_id": run["place_id"], "verification": "unverified"})
            if not pipeline.store.read(f"sources/{source_id}.json"):
                pipeline.store.save_source(source)
            run["pages"].append({"source_id": source_id, "url": page["url"], "title": source.title})
            images = ImageLinks(page["url"])
            images.feed(page["raw"])
            if name.casefold() not in source.title.casefold() and name.casefold() not in source.extracted_content.casefold() and not any(name.casefold() in (i["label"]+' '+urlsplit(i['url']).path).casefold() for i in images.images):
                raise AccessFailure("place_not_identified_on_page")
            # Explicitly labelled images first; all others remain uncertain candidates.
            links = balanced_media_links(preferred_links(images.images, name))
            for link_index, link in enumerate(links):
                if web.requests >= min(page_limit, getattr(web, 'max_requests', 45)) or len(run['candidates']) >= max_images:
                    run['deferred'].append({'url': page['url'], 'reason': 'candidate_limit' if len(run['candidates']) >= max_images else 'page_budget', 'remaining_links': len(links)-link_index})
                    break
                if link["url"] in seen or len(run["candidates"]) >= max_images:
                    continue
                seen.add(link["url"])
                explicit = name.casefold() in (link["label"]+' '+urlsplit(link['url']).path).casefold()
                if not explicit and (link["kind"] == "structured_gallery" or name.casefold() not in source.title.casefold()):
                    run["failures"].append({"url": link["url"], "reason": "image_not_linked_to_selected_place", "label": link["label"]})
                    continue
                try:
                    is_video = link.get("media_type") == "VIDEO"
                    response = web.fetch_video(link["url"]) if is_video else web.fetch_image(link["url"])
                    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/quicktime": ".mov"}[response["mime"]]
                    import hashlib
                    key = hashlib.sha256(response["data"]).hexdigest()
                    if any(c["id"] == key for c in run["candidates"]):
                        continue
                    relative = pipeline.path(choice_id, f"visual-research/media/{key}{suffix}")
                    path = pipeline.store.path(relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        temporary = path.with_suffix(suffix + ".tmp")
                        temporary.write_bytes(response["data"])
                        temporary.replace(path)
                    probe = pipeline.probe(path)
                    stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
                    width, height = stream["width"], stream["height"]
                    seconds = float(probe.get("format", {}).get("duration", 0)) if is_video else None
                    if is_video and (not seconds or not 1 <= seconds <= 600):
                        raise ValueError("video_duration_out_of_bounds")
                    quality = candidate_quality(width, height, pipeline.config, is_video)
                    if not quality["passed"]:
                        run["failures"].append({"url": response["url"], "reason": quality["reason"]})
                        continue
                    item = {"id": key, "file_path": relative, "sha256": key, "image_url": response["url"], "page_url": page["url"],
                            "source_id": source_id, "label": link["label"], "width": width, "height": height,
                            "media_type": "VIDEO" if is_video else "IMAGE", "duration": seconds, "presentation": quality['presentation'],
                            "place_link": "explicit_label" if explicit else "page_context_only",
                            "credit": link.get("credit", ""), "caption": link.get("caption", ""),
                            "date": datetime.now(UTC).isoformat(), "selected": False, "owner_verified": False}
                    run["candidates"].append(item)
                except Exception as error:
                    run["failures"].append({"url": link["url"], "reason": str(error)})
        except Exception as error:
            run["failures"].append({"url": url, "reason": str(error)})
    for result in image_results or []:
        # Assisted public image-search imports are separate from acquired page proofs.
        # Media still requires its own public-host and robots admission.
        try:
            if len(run["candidates"]) >= max_images or web.requests >= getattr(web, 'max_requests', 45):
                run['deferred'].append({'reason': 'assisted_results_budget_or_candidate_limit'})
                break
            canonical_url(result["page_url"])
            if result["place_id"] != run["place_id"]:
                raise ValueError("Résultat de recherche lié à un autre lieu.")
            is_video = result.get("media_type") == "VIDEO"
            media_url = result.get("media_url") or result.get("image_url")
            response = web.fetch_video(media_url) if is_video else web.fetch_image(media_url)
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/quicktime": ".mov"}[response["mime"]]
            import hashlib
            key = hashlib.sha256(response["data"]).hexdigest()
            relative = pipeline.path(choice_id, f"visual-research/media/{key}{suffix}")
            path = pipeline.store.path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(response["data"])
            probe = pipeline.probe(path)
            stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
            seconds = float(probe.get("format", {}).get("duration", 0)) if is_video else None
            if is_video and (not seconds or not 1 <= seconds <= 600):
                raise ValueError("video_duration_out_of_bounds")
            quality = candidate_quality(stream["width"], stream["height"], pipeline.config, is_video)
            if not quality["passed"]:
                raise ValueError(quality["reason"])
            source_id = "visual-search-" + digest(result)[:24]
            if pipeline.store.read(f"sources/{source_id}.json") is None:
                source = Source(id=source_id, brand_id=pipeline.editorial.read()["brand_id"], type=SourceType.NOTE,
                                title=result["label"], url_or_path=result["page_url"], raw_content=json.dumps(result, ensure_ascii=False),
                                extracted_content="URL-image rattachée par un index public ; page originale non acquise, identité à contrôler.",
                                metadata={"origin": "assisted_public_image_search", "verification": "unverified", "page_acquired": False})
                pipeline.store.save_source(source)
            run["candidates"].append({"id": key, "file_path": relative, "sha256": key, "image_url": response["url"],
                                      "page_url": result["page_url"], "source_id": source_id, "label": result["label"],
                                      "width": stream["width"], "height": stream["height"], "place_link": "external_search_result",
                                      "media_type": "VIDEO" if is_video else "IMAGE", "duration": seconds, "presentation": quality['presentation'],
                                      "date": datetime.now(UTC).isoformat(), "selected": False, "owner_verified": False,
                                      "credit": result.get("credit", ""), "publisher_url": result.get("publisher_url"), "page_acquired": False})
        except Exception as error:
            run["failures"].append({"url": result.get("media_url") or result.get("image_url"), "reason": str(error)})
    run["source_publishers"] = sorted({key for c in run["candidates"] if (key := source_identity(c))})
    run["sourcing_complete"] = len(run["source_publishers"]) >= 2
    run["http_requests"] = web.requests
    pipeline.store.write(pipeline.path(choice_id, f"visual-research/runs/{run['id']}.json"), run, immutable=True)
    with pipeline.transaction(choice_id) as project:
        project.setdefault("visual_research", []).append(run)
        pipeline.event(project, "visual_research", "assistant", {"run_id": run["id"], "candidates": len(run["candidates"]), "failures": run["failures"]})
    return run


def select(pipeline, choice_id, candidate_id, beat, note, *, actor="assistant", role="assistant", clip_in=None, clip_out=None, presentation=None):
    project = pipeline.read(choice_id)
    candidate = next(c for r in project.get("visual_research", []) for c in r["candidates"] if c["id"] == candidate_id)
    if not note.strip():
        raise ValueError("Justification de correspondance au segment requise.")
    review = project.get("visual_reviews", {}).get(candidate_id)
    if not review or review["decision"] != "accepted":
        raise ValueError("Inspection visuelle assistant acceptée requise ; une URL ou un titre ne suffit pas.")
    path = pipeline.store.path(candidate["file_path"])
    if file_hash(path) != candidate["sha256"]:
        raise ValueError("Candidat visuel modifié.")
    mode = presentation if presentation is not None else candidate.get('presentation', 'cover')
    from app.services.visual_presentation import presentation as check_presentation
    check_presentation({'type': candidate.get('media_type', 'IMAGE'), 'metadata': {'source': {'presentation': mode}}})
    probe = pipeline.probe(path)
    stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    quality = native_quality(stream['width'], stream['height'], pipeline.config,
                             {} if candidate.get('media_type') == 'VIDEO' else None, presentation=mode)
    if not quality['passed']:
        raise ValueError(quality['reason'])
    candidate = {**candidate, 'presentation': mode}
    evidence_hash = file_hash(pipeline.store.path(f"sources/{candidate['source_id']}.json"))
    if candidate.get("media_type") == "VIDEO":
        start, end = float(clip_in or 0), float(clip_out if clip_out is not None else candidate["duration"])
        if not 0 <= start < end <= candidate["duration"]:
            raise ValueError("Extrait hors de la durée de la vidéo.")
        candidate = {**candidate, "clip_in": start, "clip_out": end}
    return pipeline.upload(choice_id, beat, path.name, path.read_bytes(), actor, note,
                           role=role, source={**candidate, "selection_reason": note, "evidence_sha256": evidence_hash})


def review_visual(pipeline, choice_id, candidate_id, decision, note, *, sharpness=None):
    if decision not in {"accepted", "rejected"} or not note.strip() or (sharpness is not None and type(sharpness) is not bool):
        raise ValueError("Inspection visuelle motivée requise.")
    with pipeline.transaction(choice_id) as project:
        if not any(c["id"] == candidate_id for r in project.get("visual_research", []) for c in r["candidates"]):
            raise ValueError("Candidat inconnu.")
        review = {"decision": decision, "note": note, "actor": "assistant", "at": datetime.now(UTC).isoformat(), "owner_verified": False}
        review["sharpness"] = sharpness
        project.setdefault("visual_reviews", {})[candidate_id] = review
        pipeline.event(project, "assistant_visual_review", "assistant", {"candidate_id": candidate_id, **review})
        return review


def import_browser_bundle(pipeline, choice_id, manifest, context):
    """Import exact files exported by the browser's observed-assets capability.

    No network fetch, cookie copying or HTML-page acquisition claim. The UI
    observation and bundle manifest are retained as an assistant Source NOTE.
    """
    dossier, _ = pipeline.dossier(choice_id)
    if context["place_id"] != dossier["place"]["id"] or not context.get("observed"):
        raise ValueError("Contexte de la page et identité du lieu requis.")
    canonical_url(context["page_url"])
    run = {"id": digest({"manifest": manifest, "context": context})[:32], "place_id": context["place_id"],
           "pages": [], "candidates": [], "failures": [], "http_requests": 0, "paid_calls": 0,
           "acquisition": "assistant_browser_observed_assets", "notice": "Observation assistant du navigateur ; aucun contrôle propriétaire."}
    if any(r["id"] == run["id"] for r in pipeline.read(choice_id).get("visual_research", [])):
        return next(r for r in pipeline.read(choice_id)["visual_research"] if r["id"] == run["id"])
    source_id = "visual-browser-" + run["id"][:24]
    source = Source(id=source_id, brand_id=pipeline.editorial.read()["brand_id"], type=SourceType.NOTE,
                    title=context["label"], url_or_path=context["page_url"], raw_content=json.dumps({"context": context, "manifest": manifest}, ensure_ascii=False),
                    extracted_content=context["observed"], metadata={"origin": run["acquisition"], "page_acquired": False,
                    "rendered_ui_observed": True, "verification": "unverified"})
    if not pipeline.store.read(f"sources/{source_id}.json"):
        pipeline.store.save_source(source)
    for item in manifest["assets"]:
        try:
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/quicktime": ".mov"}[item["contentType"]]
            canonical_url(item["url"])
            original = Path(item["path"])
            if not 0 < original.stat().st_size <= 100 * 1024 * 1024:
                raise ValueError("Fichier navigateur trop grand ou vide.")
            key = file_hash(original)
            relative = pipeline.path(choice_id, f"visual-research/media/{key}{suffix}")
            target = pipeline.store.path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_suffix(suffix + ".tmp")
                temporary.write_bytes(original.read_bytes())
                temporary.replace(target)
            probe = pipeline.probe(target)
            stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
            quality = candidate_quality(stream["width"], stream["height"], pipeline.config, item["contentType"].startswith("video/"))
            if not quality["passed"]:
                raise ValueError(quality["reason"])
            video = item["contentType"].startswith("video/")
            seconds = float(probe.get("format", {}).get("duration", 0)) if video else None
            if video and (not seconds or not 1 <= seconds <= 600):
                raise ValueError("Durée vidéo invalide.")
            detail = context.get("asset_details", {}).get(item["url"], {})
            run["candidates"].append({"id": key, "sha256": key, "file_path": relative, "image_url": item["url"],
                "page_url": context["page_url"], "source_id": source_id, "label": detail.get("label", context["label"]), "credit": detail.get("credit", context.get("credit", "")),
                "width": stream["width"], "height": stream["height"], "media_type": "VIDEO" if video else "IMAGE", "duration": seconds, "presentation": quality['presentation'],
                "displayed_date": detail.get("displayed_date", context.get("displayed_date")), "place_link": "browser_gallery_context", "page_acquired": False,
                "publisher_url": detail.get("publisher_url", context.get("publisher_url")),
                "rendered_ui_observed": True, "date": datetime.now(UTC).isoformat(), "owner_verified": False})
        except Exception as error:
            run["failures"].append({"url": item.get("url"), "reason": str(error)})
    pipeline.store.write(pipeline.path(choice_id, f"visual-research/runs/{run['id']}.json"), run, immutable=True)
    with pipeline.transaction(choice_id) as project:
        project.setdefault("visual_research", []).append(run)
        pipeline.event(project, "visual_browser_imported", "assistant", {"run_id": run["id"], "context": context})
    return run


def main():
    import argparse
    from app.services.editorial_workflow import EditorialWorkflow
    from app.services.reel_pipeline import ReelPipeline
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/example/editorial-workflow")
    parser.add_argument("--choice", required=True)
    parser.add_argument("--page", action="append", default=[])
    parser.add_argument("--select")
    parser.add_argument("--beat", type=int)
    parser.add_argument("--note")
    parser.add_argument("--clip-in", type=float)
    parser.add_argument("--clip-out", type=float)
    parser.add_argument("--max-images", type=int, default=12)
    parser.add_argument("--image-results", help="Import attribué d'URLs réellement vues dans une recherche publique, pas preuves de page acquises.")
    parser.add_argument("--browser-bundle", help="Manifeste des fichiers observés et exportés par le navigateur.")
    parser.add_argument("--browser-context", help="Contexte public de la galerie observée par l'assistant.")
    args = parser.parse_args()
    pipeline = ReelPipeline(EditorialWorkflow(args.output))
    if args.select:
        value = select(pipeline, args.choice, args.select, args.beat, args.note, clip_in=args.clip_in, clip_out=args.clip_out)
        print(json.dumps({"place": value["place_id"], "selected_beat": args.beat, "owner_validated": False}))
    elif args.browser_bundle:
        if not args.browser_context:
            parser.error("Contexte public de la galerie requis.")
        value = import_browser_bundle(pipeline, args.choice, json.loads(Path(args.browser_bundle).read_text()), json.loads(Path(args.browser_context).read_text()))
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        if not args.page and not args.image_results:
            parser.error("Au moins une page publique de preuve requise.")
        results = json.loads(Path(args.image_results).read_text()) if args.image_results else None
        run = research(pipeline, args.choice, args.page, max_images=args.max_images, image_results=results)
        print(json.dumps(run, ensure_ascii=False, indent=2))
        if run["failures"] and not run["candidates"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
