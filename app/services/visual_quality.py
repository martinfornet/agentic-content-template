"""Mandatory Agentic Content picture contract, checked before rendering and independently at delivery.

Native Full HD after the actual vertical crop, one use per photograph, and at
least two originating publishers/accounts. File names and CDN hosts are not
photograph identities or editorial sources. These floors cannot be disabled by
an old project/config. A technical pass is not an artistic or rights approval.
"""
import hashlib
import math
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VERSION = "content-visual-quality-2026-10-29-landscape-proposal"
MIN_WIDTH, MIN_HEIGHT, MIN_SOURCES = 1080, 1920, 2


def native_quality(width, height, config, motion=None, *, presentation='cover'):
    if presentation == 'photo_fit':
        from app.services.visual_presentation import full_photo_quality
        return full_photo_quality(width, height, config)
    if presentation == 'landscape_fit':
        from app.services.visual_presentation import landscape_quality
        return landscape_quality(width, height, config)
    if presentation != 'cover':
        raise ValueError('Présentation visuelle inconnue.')
    policy = config.get("source_quality") or {}
    minimum_width = max(MIN_WIDTH, policy.get("min_crop_width", MIN_WIDTH))
    minimum_height = max(MIN_HEIGHT, policy.get("min_crop_height", MIN_HEIGHT))
    motion = config.get("photo_motion", {}) if motion is None else motion
    zoom = motion.get("maximum_zoom", 1.035) if motion.get("enabled") else 1
    if not math.isfinite(zoom) or not 1 <= zoom <= 1.08:
        raise ValueError("Mouvement photo hors limites.")
    crop_width, crop_height = min(width, height * 9 / 16) / zoom, min(height, width * 16 / 9) / zoom
    passed = crop_width >= minimum_width and crop_height >= minimum_height
    return {"width": width, "height": height, "crop_width": int(crop_width), "crop_height": int(crop_height),
            "resolution_passed": passed, "passed": passed, "maximum_zoom": zoom,
            "upscale_to_output": max(MIN_WIDTH / max(1, crop_width), MIN_HEIGHT / max(1, crop_height)),
            "label": "4K native pour le cadre vertical" if crop_width >= 2160 and crop_height >= 3840 else "Full HD native pour le cadre vertical" if passed else "Sous le minimum Full HD vertical",
            "reason": None if passed else f"Source insuffisante après recadrage et zoom : {int(crop_width)}×{int(crop_height)} ; minimum {minimum_width}×{minimum_height} natifs. Rechercher une meilleure photo ; aucun agrandissement ni fond de remplissage."}


def normalized_url(value):
    p = urlsplit(value or "")
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        return None
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def source_identity(source):
    """Count originating pages/accounts, never individual CDN files or articles."""
    page = normalized_url(source.get("page_url"))
    if not page:
        return None
    host = urlsplit(page).hostname.lower().removeprefix("www.")
    labels = host.split(".")
    size = 3 if host.endswith((".co.uk", ".com.au", ".co.jp")) else 2
    domain = ".".join(labels[-size:])
    if domain in {"instagram.com", "facebook.com", "tiktok.com", "youtube.com"}:
        # A post URL alone does not identify its author; retain a verified profile URL.
        account = normalized_url(source.get("publisher_url") or page)
        if not account or urlsplit(account).hostname.lower().removeprefix("www.") != domain:
            return None
        path = urlsplit(account).path.strip("/").split("/")
        if not path[0] or path[0].lower() in {"p", "reel", "reels", "watch", "shorts", "photo", "photos", "stories", "share"}:
            return None
        return domain + "/" + path[0].casefold()
    return domain


def image_identity(value):
    url = normalized_url(value)
    if not url:
        return None
    p = urlsplit(url)
    transforms = {'w', 'h', 'width', 'height', 'q', 'quality', 'fit', 'crop', 'fm', 'format', 'auto', 'dpr'}
    query = urlencode(sorted((k,v) for k,v in parse_qsl(p.query) if k.lower() not in transforms))
    return urlunsplit((p.scheme, p.netloc, p.path, query, ''))


def ordered_assets(project):
    from app.services.reel_editing import shot_sequence, visual_assets
    assets = visual_assets(project)
    plan = project.get("visual_plan") or [{} for _ in project['draft']['beats']]
    if len(plan) != len(project['draft']['beats']):
        raise ValueError("Découpage à réviser après modification du récit.")
    return [assets[s.get('asset_index',s.get('asset_beat'))] for i, slot in enumerate(plan) for s in shot_sequence(slot, i, len(assets))]


def image_fingerprint(path, ffmpeg="ffmpeg"):
    # Decode pixels, rather than comparing compressed bytes. Same photograph
    # re-exported as JPEG/WebP or resized remains detectable without new libraries.
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-vf", "scale=32:32:flags=area,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"],
                         capture_output=True, check=True, timeout=30).stdout
    if len(raw) != 32 * 32 * 3:
        raise ValueError("Photo illisible : contrôle des pixels impossible.")
    gray = [sum(raw[i:i+3]) / 3 for i in range(0, len(raw), 3)]
    bits = [gray[y*32+x] > gray[y*32+x+1] for y in range(0, 32, 2) for x in range(0, 30, 2)]
    return {"pixels": raw, "bits": bits, "pixel_sha256": hashlib.sha256(raw).hexdigest()}


def same_photo(a, b):
    if a["pixel_sha256"] == b["pixel_sha256"]:
        return True
    distance = sum(x != y for x, y in zip(a["bits"], b["bits"]))
    error = sum(abs(x-y) for x, y in zip(a["pixels"], b["pixels"])) / len(a["pixels"])
    return distance <= 12 and error <= 8


def audit(pipeline, project, *, assets=None, deep=False):
    """Fast metadata checks for UI; exact bytes, provenance and pixels for production."""
    from app.services.reel_pipeline import file_hash
    errors, rows, origins, photo_origins, photographs = [], [], set(), set(), []
    video_clips = []
    assets = ordered_assets(project) if assets is None else assets
    from app.services.visual_reuse import authorization, permits
    exceptions = []
    try:
        reuse = authorization(pipeline, project)
    except (ValueError, KeyError, OSError) as error:
        reuse = None
        errors.append(str(error))
    if not assets:
        errors.append("Aucun plan visuel documenté.")
    for i, asset in enumerate(assets):
        label = f"Plan {i+1} : "
        if not asset:
            errors.append(label + "photo à sourcer.")
            continue
        meta = asset.get("metadata", {})
        source = meta.get("source") or {}
        origin = source_identity(source)
        if not origin or not source.get("source_id") or not source.get("evidence_sha256"):
            errors.append(label + "provenance documentée requise (page d’origine, auteur/compte et preuve conservée).")
        if origin:
            origins.add(origin)
            if asset['type'] == 'IMAGE':
                photo_origins.add(origin)
        inspection = project.get("visual_reviews", {}).get(source.get("id") or meta.get("sha256"), {})
        if inspection.get("decision") != "accepted" or inspection.get("sharpness") is not True:
            errors.append(label + "netteté et correspondance au lieu à inspecter sur le fichier exact.")
        quality = pipeline.source_quality(asset, project)
        if not quality["passed"]:
            errors.append(label + quality["reason"])
        row = {"shot": i+1, "asset": asset["file_path"], "sha256": meta.get("sha256"), "page_url": source.get("page_url"), "publisher": origin, "quality": quality}
        identity = {"sha256": meta.get("sha256"), "original": source.get("source_sha256") or source.get("sha256"), "url": image_identity(source.get("image_url")), "path": asset["file_path"]}
        fingerprint = None
        if deep:
            try:
                path = pipeline.store.path(asset["file_path"])
                if not path.is_file() or file_hash(path) != meta.get("sha256"):
                    raise ValueError("fichier absent ou empreinte modifiée.")
                probe = pipeline.probe(path)
                stream = next(s for s in probe["streams"] if s.get("codec_type") == "video")
                if asset['type'] == 'VIDEO':
                    duration = float(probe.get('format', {}).get('duration', 0))
                    if not 0 <= float(source.get('clip_in', -1)) < float(source.get('clip_out', -1)) <= duration:
                        raise ValueError('extrait vidéo hors de la durée du fichier.')
                from app.services.visual_presentation import presentation
                actual = native_quality(stream["width"], stream["height"], pipeline.config,
                                        project.get("photo_motion") if asset["type"] == "IMAGE" else {}, presentation=presentation(asset))
                if not actual['passed']:
                    raise ValueError(actual['reason'])
                declared = next(s for s in meta['probe']['streams'] if s.get('codec_type') == 'video')
                if (stream['width'], stream['height']) != (declared['width'], declared['height']):
                    raise ValueError('dimensions source déclarées différentes du fichier exact.')
                if not actual["passed"]:
                    raise ValueError(actual["reason"])
                proof = pipeline.store.path("sources/" + source["source_id"] + ".json")
                if file_hash(proof) != source["evidence_sha256"]:
                    raise ValueError("preuve de provenance modifiée.")
                evidence = pipeline.store.read("sources/" + source["source_id"] + ".json")
                if normalized_url(evidence.get("url_or_path")) != normalized_url(source.get("page_url")):
                    raise ValueError("la preuve conservée ne correspond pas à la page d’origine.")
                if asset["type"] == "IMAGE":
                    fingerprint = image_fingerprint(path, pipeline.config["ffmpeg"])
                    row["pixel_sha256"] = fingerprint["pixel_sha256"]
                row["quality"] = actual
            except (ValueError, KeyError, OSError, StopIteration, subprocess.SubprocessError) as error:
                errors.append(label + str(error))
        if asset["type"] == "IMAGE":
            for previous, previous_fp, position in photographs:
                duplicate = any(identity[k] and identity[k] == previous[k] for k in identity)
                if duplicate or (fingerprint and previous_fp and same_photo(fingerprint, previous_fp)):
                    if permits(reuse, previous, identity, position, i+1, len(assets)):
                        exceptions.append(reuse)
                        row['owner_reuse_review_id'] = reuse['review_id']
                    else:
                        errors.append(label + f"photo déjà utilisée au plan {position}. Sourcer une photo différente ; renommer ou réencoder ne suffit pas.")
                    break
            photographs.append((identity, fingerprint, i+1))
        elif asset['type'] == 'VIDEO':
            start, end = source.get('clip_in'), source.get('clip_out')
            if not all(type(t) in (int, float) and math.isfinite(t) for t in (start, end)) or not 0 <= start < end:
                errors.append(label + 'bornes finies clip_in/clip_out requises pour chaque extrait vidéo.')
            else:
                row.update(clip_in=start, clip_out=end)
                for previous, pstart, pend, position in video_clips:
                    if any(identity[k] and identity[k] == previous[k] for k in identity) and max(start, pstart) < min(end, pend):
                        errors.append(label + f'extrait vidéo déjà utilisé au plan {position} : aucun chevauchement ni boucle autorisé.')
                video_clips.append((identity, start, end, i+1))
        else:
            errors.append(label + 'type de média non pris en charge.')
        rows.append(row)
    if len(origins) < MIN_SOURCES:
        errors.append(f"Sourcing insuffisant : {len(origins)} source distincte ; au moins {MIN_SOURCES} éditeurs/comptes d’origine requis. Plusieurs pages du même site comptent pour une source.")
    if len(photographs) > 1 and len(photo_origins) < MIN_SOURCES:
        errors.append("Les photos doivent elles-mêmes provenir d’au moins deux sources distinctes ; une vidéo d’un autre site ne suffit pas.")
    return {"policy_version": VERSION, "passed": not errors, "errors": list(dict.fromkeys(errors)), "sources": sorted(origins), "photo_sources": sorted(photo_origins), "shots": rows, "deep_verified": deep, "owner_reuse_exceptions": exceptions}


def require(report):
    if not report["passed"]:
        raise ValueError("Sourcing visuel à compléter : " + " ".join(report["errors"]))
    return report


def frame_sample(path, ffmpeg, picture_filter='', timestamp=None, window='crop=1080:900:0:0'):
    args = [ffmpeg, '-v', 'error']
    if timestamp is not None:
        args += ['-ss', str(timestamp)]
    # Upper image area is outside the studio's caption band (y >= 1190).
    sample = window+',scale=48:40:flags=area,format=rgb24'
    args += ['-i', str(path), '-vf', picture_filter + (',' if picture_filter else '') + sample,
             '-frames:v', '1', '-f', 'rawvideo', '-']
    data = subprocess.run(args, capture_output=True, check=True, timeout=30).stdout
    if len(data) != 48*40*3:
        raise ValueError('Impossible de vérifier les photos dans la vidéo encodée.')
    return data


def verify_run(pipeline, project, run, *, video=None):
    """Recompute from the exact delivered timeline; do not trust a 'passed' flag."""
    from app.services.reel_editing import photo_filter
    from app.services.visual_presentation import asset_filter, presentation, sample_window
    inputs, timeline = run.get("inputs", {}), run.get("timeline", [])
    if not timeline:
        raise ValueError("Chronologie des photos absente : livraison refusée.")
    assets, selected, cursor = inputs.get("assets", []), [], 0
    motion = inputs.get("photo_motion", pipeline.config.get("photo_motion"))
    for shot in timeline:
        index = shot.get("asset_index")
        if type(index) is not int or not 0 <= index < len(assets):
            raise ValueError("Chaque plan livré doit référencer son fichier source exact.")
        asset = assets[index]
        if asset["file_path"] != shot.get("asset"):
            raise ValueError("Le plan livré ne correspond pas au fichier source documenté.")
        start, end = shot.get("start"), shot.get("end")
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (start, end)) or end <= start or abs(start-cursor) > .04:
            raise ValueError("Chronologie visuelle discontinue ou invalide.")
        expected = asset_filter(asset, end-start, motion)
        if asset['type'] == 'VIDEO':
            source = asset.get('metadata', {}).get('source', {})
            if end-start > source.get('clip_out', 0)-source.get('clip_in', 0)+.001:
                raise ValueError('Extrait vidéo trop court pour le plan : aucune boucle ni image figée de remplissage.')
        if shot.get("visual_filter") != expected:
            raise ValueError("Présentation non documentée : plein écran ou photo paysage plein large explicitement sélectionnée requis.")
        selected.append(asset)
        cursor = end
    if abs(cursor - run.get("technical", {}).get("duration", -1)) > .25:
        raise ValueError("Les sources ne couvrent pas toute la durée du Reel.")
    from app.services.reel_rhythm import verify_timeline
    rhythm = verify_timeline(project, timeline)
    report = require(audit(pipeline, dict(project, photo_motion=motion or {},
                                         photo_reuse_review_id=inputs.get('photo_reuse_review_id')), assets=selected, deep=True))
    report['rhythm'] = rhythm
    report['rendered_frames_verified'] = False
    if video is not None:
        samples = []
        for i, (asset, shot) in enumerate(zip(selected, timeline)):
            seconds = shot['end'] - shot['start']
            for fraction in (.25, .75):
                expected_filter = asset_filter(asset, seconds, None)
                if asset['type'] == 'IMAGE' and presentation(asset) == 'cover' and motion and motion.get('enabled'):
                    frames = max(1, round(seconds*30))
                    zoom = 1 + (motion.get('maximum_zoom',1.035)-1)*int(seconds*fraction*30)/max(1,frames-1)
                    expected_filter += f",zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=30"
                source_time = None
                if asset['type'] == 'VIDEO':
                    source_time = asset['metadata']['source']['clip_in']
                    # Match the render's seek/FPS origin, then select its frame.
                    # A second absolute seek changes the 25-to-30fps phase.
                    frame_index = math.ceil(seconds*fraction*30-1e-6)
                    expected_filter += f',select=eq(n\\,{frame_index})'
                window = sample_window(asset)
                expected = frame_sample(pipeline.store.path(asset['file_path']),pipeline.config['ffmpeg'],expected_filter,timestamp=source_time,window=window)
                actual = frame_sample(video,pipeline.config['ffmpeg'],timestamp=shot['start']+seconds*fraction,window=window)
                error = sum(abs(a-b) for a,b in zip(expected,actual))/len(actual)
                if error > 12:
                    raise ValueError(f'Plan {i+1} : la photo réellement encodée ne correspond pas au cadrage documenté. Livraison refusée.')
                samples.append({'shot':i+1,'seconds':shot['start']+seconds*fraction,'pixel_error':round(error,3)})
        report.update(rendered_frames_verified=True,frame_samples=samples)
    return report
