"""Resumable, offline Reel production with explicit owner approval and final QA."""
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import zipfile
from uuid import uuid4

from app.models.asset import Asset, AssetType
from app.models.content import Content, ContentStatus, ContentVersion
from app.models.feedback import Feedback, ReviewAction
from app.services.acquisition_store import AcquisitionStore
from app.services.reel_copy import propose


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_HASH_CACHE = {}


def dependency_hash(path):
    path = Path(path)
    if not path.is_file():
        return "missing"
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if key not in _HASH_CACHE:
        _HASH_CACHE[key] = file_hash(path)
    return _HASH_CACHE[key]


def content_from(value):
    data = dict(value)
    data["status"] = ContentStatus(data["status"])
    data["versions"] = [ContentVersion(**v) for v in data["versions"]]
    data["feedback"] = [Feedback(**{**v, "action": ReviewAction(v["action"])}) for v in data["feedback"]]
    return Content(**data)


QA_POINTS = {"factual": "Les affirmations du Reel et de la légende correspondent aux contrôles.",
             "visuals": "Visuels du bon lieu, nets et cadrés correctement en vertical.",
             "voice": "Voix écoutée intégralement : prononciation, naturel, absence de craquements.",
             "subtitles": "Sous-titres lisibles, exacts et synchronisés sur chaque segment.",
             "assembly": "Vidéo regardée intégralement : pas de trou, écran noir ni rupture gênante.",
             "caption": "Légende relue, informations pratiques et appréciations attribuées."}


class ReelPipeline:
    def __init__(self, editorial, root=None, config=None):
        self.editorial = editorial
        state = editorial.read()
        self.repository = Path(state["repository"])
        self.store = AcquisitionStore(root or editorial.store.root / "reels")
        self.config = config or json.loads((self.repository / "brands/example/reel.json").read_text())
        if (self.config["width"], self.config["height"], self.config["fps"]) != (1080, 1920, 30):
            raise ValueError("Le premier format Reel est fixé à 1080×1920, 30 images/s.")

    def path(self, project_id, tail="project.json"):
        if not project_id.isalnum():
            raise ValueError("Identifiant de choix invalide.")
        return f"projects/{project_id}/{tail}"

    def read(self, project_id):
        value = self.store.read(self.path(project_id))
        if value is None:
            raise ValueError("Préparer la proposition avant cette action.")
        return value

    @contextmanager
    def transaction(self, project_id):
        lockpath = self.store.path(self.path(project_id, "project.lock"))
        lockpath.parent.mkdir(parents=True, exist_ok=True)
        with lockpath.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            project = self.store.read(self.path(project_id))
            yield project
            if project is not None:
                self.store.write(self.path(project_id), project)

    def event(self, project, kind, actor, payload):
        project["history"].append({"id": uuid4().hex, "kind": kind, "actor": actor,
                                   "recorded_at": datetime.now(UTC).isoformat(), "payload": payload})

    def studio_project(self, review_id):
        """Create a review montage from an actual studio decision, never a publication choice."""
        import copy
        from app.services.editorial_inbox import EditorialInbox
        from app.services.reel_story import StoryPreparation
        reviews = EditorialInbox(self.editorial, self).list()
        review = next((r for r in reviews if r['id'] == review_id), None)
        if not review or review['kind'] != 'story' or review['decision'] != 'keep':
            raise ValueError('Validation réelle du récit requise.')
        story = StoryPreparation(self.editorial).latest(review['place_id'])
        last = [r for r in reviews if r['place_id'] == review['place_id']][-1]
        if last['id'] != review_id or not story or story.get('stale') or story['id'] != review['target_id'] or story.get('audition', {}).get('sha256') != review.get('audition_sha256'):
            raise ValueError('Validation remplacée ou voix modifiée.')
        ident = review_id
        existing = self.store.read(self.path(ident))
        if existing:
            self.dossier(ident)
            return existing
        draft = copy.deepcopy(story['draft'])
        place = self.editorial.dossier(review['place_id'])['place']
        self.validate_copy(draft, place)
        content = Content(brand_id=self.editorial.read()['brand_id'], idea_id=ident,
                          format='instagram_reel_script', title=place['name'], body=json.dumps(draft, ensure_ascii=False))
        content.mark_drafted()
        content.mark_qa_passed({'check': 'structure_and_evidence_references_only', 'facts_verified': False})
        content.await_review()
        proof = dict(story_id=story['id'], review_id=review_id, sha256=review['audition_sha256'],
                     audio_path=story['audition']['audio_path'], draft_sha256=digest(draft))
        if review.get('music'):
            proof['music'] = copy.deepcopy(review['music'])
        project = dict(id=ident, place_id=review['place_id'], studio_review_id=review_id,
                       format=self.config['format'], content=content.to_dict(), draft=draft,
                       approval=None, visual_lot_approval=None, approved_narration=proof,
                       photo_motion={'enabled': False}, assets={}, runs=[], exports=[], history=[])
        self.event(project, 'studio_montage_requested', 'assistant', {'review_id': review_id})
        self.store.write(self.path(ident), project)
        return project

    def dossier(self, choice_id, *, allow_historical=False):
        project = self.store.read(self.path(choice_id))
        if project and project.get('studio_review_id'):
            from app.services.editorial_inbox import EditorialInbox
            from app.services.reel_story import StoryPreparation
            rows = [r for r in EditorialInbox(self.editorial, self).list() if r['place_id'] == project['place_id']]
            original = next((r for r in rows if r['id'] == project['studio_review_id']), None)
            latest = rows[-1] if rows else None
            story = StoryPreparation(self.editorial).latest(project['place_id'])
            run_correction = latest and latest['kind'] == 'run' and latest['decision'] == 'changes' and any(r['id'] == latest['target_id'] for r in project['runs'])
            proof = project.get('approved_narration', {})
            original_valid = bool(original and original['decision'] == 'keep' and original['kind'] == 'story'
                                  and proof.get('sha256') == original.get('audition_sha256')
                                  and proof.get('music') == original.get('music')
                                  and digest(project['draft']) == proof.get('draft_sha256')
                                  and (latest['id'] == original['id'] or run_correction))
            unchanged = bool(story and not story.get('stale') and story['id'] == original.get('target_id')
                             and story.get('audition', {}).get('sha256') == original.get('audition_sha256')) if original else False
            revision = None
            from app.services.revision_active import active_revision
            active = active_revision(project, latest)
            if original_valid and run_correction and active == 'revision_mix_proposal':
                from app.services.reel_revision_mix import mix_candidate
                revision = mix_candidate(self, project, latest)
            elif original_valid and run_correction and active == 'revision_story_proposal':
                from app.services.reel_revision_story import story_candidate
                revision = story_candidate(self, project, latest)
            elif original_valid and run_correction and active == 'revision_voice_proposal':
                from app.services.reel_revision_voice import removed_beat_candidate
                revision = removed_beat_candidate(self, project, latest)
            if not original_valid or not (unchanged or revision):
                raise ValueError('Validation du studio remplacée ou version modifiée.')
            return self.editorial.dossier(project['place_id']), {'choice_id': choice_id, 'current_blockers': [], 'scoped_checks_complete': False, 'latest_checks': {}}
        state = self.editorial.read()
        choice = next((e for e in state["history"] if e["id"] == choice_id and e["kind"] == "publication_choice"), None)
        if not choice:
            raise ValueError("Aucun choix propriétaire correspondant.")
        dossier = self.editorial.dossier(choice["payload"]["place_id"])
        summary = next(s for s in dossier["choices"] if s["choice_id"] == choice_id)
        if summary["current_blockers"] and not allow_historical:
            raise ValueError(" ; ".join(summary["current_blockers"]))
        return dossier, summary

    def owner(self, actor):
        self.editorial.owner(self.editorial.read(), actor, "owner")

    def validate_copy(self, draft, place):
        if not all(isinstance(draft.get(k), str) and draft[k].strip() for k in ("angle", "caption")):
            raise ValueError("Angle et légende obligatoires.")
        beats = draft.get("beats", [])
        if not 4 <= len(beats) <= 12:
            raise ValueError("Format unique : 4 à 12 segments parlés.")
        for beat in beats:
            if not isinstance(beat.get("text"), str) or not 1 <= len(beat["text"].strip()) <= 220:
                raise ValueError("Chaque segment doit contenir 1 à 220 caractères.")
            if not isinstance(beat.get("points"), list) or not isinstance(beat.get("source_ids"), list):
                raise ValueError("Liens aux points et Sources requis pour chaque segment.")
            if not set(beat["points"]).issubset(place["points"]):
                raise ValueError("Point inconnu dans le script.")
            if not set(beat["source_ids"]).issubset({s["id"] for s in place["sources"]}):
                raise ValueError("Source inconnue dans le script.")
            if beat["points"] and not beat["source_ids"]:
                raise ValueError("Affirmation : au moins une Source obligatoire.")
        # User can add caption-only claims via explicit used_points; no silent omission.
        declared = draft.get("used_points", [])
        if not isinstance(declared, list) or not set(declared).issubset(place["points"]):
            raise ValueError("Périmètre factuel invalide.")
        draft["used_points"] = sorted(set(declared) | {p for b in beats for p in b["points"]})
        return draft

    def initial_copy(self, place):
        from app.services.reel_story import StoryPreparation
        prepared = StoryPreparation(self.editorial).latest(place['id'])
        if prepared and not prepared['stale']:
            return prepared['draft']
        if self.config.get('prepared_story_required', False):
            raise ValueError('Préparer un récit sourcé actuel pour ce lieu avant de créer ou renouveler le script.')
        return propose(place)

    def draft(self, choice_id):
        dossier, summary = self.dossier(choice_id)
        with self.transaction(choice_id) as existing:
            if existing:
                return existing
            draft = self.validate_copy(self.initial_copy(dossier["place"]), dossier["place"])
            content = Content(brand_id=self.editorial.read()["brand_id"], idea_id=choice_id,
                              format="instagram_reel_script", title=dossier["place"]["name"], body=json.dumps(draft, ensure_ascii=False))
            content.mark_drafted()
            content.mark_qa_passed({"check": "structure_and_evidence_references_only", "facts_verified": False})
            content.await_review()
            project = {"id": choice_id, "place_id": dossier["place"]["id"], "format": self.config["format"],
                       "content": content.to_dict(), "draft": draft, "approval": None, "assets": {}, "runs": [],
                       "exports": [], "history": []}
            self.event(project, "draft_proposed", "assistant", {"version": 1, "draft": draft,
                                                                "dossier": dossier["place"], "choice": summary})
            self.store.write(self.path(choice_id), project)
            return project

    def edit(self, choice_id, draft, actor):
        self.owner(actor)
        dossier, _ = self.dossier(choice_id, allow_historical=True)
        draft = self.validate_copy(draft, dossier["place"])
        with self.transaction(choice_id) as project:
            content = content_from(project["content"])
            # Editing approved/produced content always creates a new unapproved version.
            content.status = ContentStatus.AWAITING_REVIEW
            content.edit(json.dumps(draft, ensure_ascii=False), comment="Correction humaine angle/script/légende.")
            project["draft"], project["content"], project["approval"] = draft, content.to_dict(), None
            self.event(project, "draft_edited", actor, {"version": content.version, "draft": draft})
            return project

    def repropose(self, choice_id, draft=None):
        dossier, _ = self.dossier(choice_id, allow_historical=True)
        with self.transaction(choice_id) as project:
            if project["approval"]:
                raise ValueError("Corriger la version approuvée avant une nouvelle proposition.")
            draft = self.validate_copy(self.initial_copy(dossier["place"]) if draft is None else draft, dossier["place"])
            content = content_from(project["content"])
            content.version += 1
            content.body = json.dumps(draft, ensure_ascii=False)
            content.versions.append(ContentVersion(version=content.version, body=content.body, author="ai", note="Nouvelle proposition locale sourcée."))
            content.status = ContentStatus.AWAITING_REVIEW
            project.update(draft=draft, content=content.to_dict())
            self.event(project, "draft_reproposed", "assistant", {"version": content.version, "draft": draft})
            return project

    def verification_signature(self, summary):
        return digest({"choice_id": summary["choice_id"], "checks": summary["latest_checks"]})

    def approve(self, choice_id, actor, note):
        self.owner(actor)
        dossier, summary = self.dossier(choice_id)
        if not summary["scoped_checks_complete"]:
            raise ValueError("Vérifications du choix encore ouvertes : " + ", ".join(summary["remaining_points"]))
        if not note.strip():
            raise ValueError("Note d'approbation explicite requise.")
        with self.transaction(choice_id) as project:
            for point in project["draft"]["used_points"]:
                if summary["latest_checks"].get(point, {}).get("result") != "verified":
                    raise ValueError(f"Le script/la légende utilise {point} : un contrôle verified est requis.")
            content = content_from(project["content"])
            content.status = ContentStatus.AWAITING_REVIEW
            content.approve(note)
            content.qa_information["scoped_fact_checks_signature"] = self.verification_signature(summary)
            project["content"] = content.to_dict()
            project["approval"] = {"actor": actor, "note": note, "version": content.version,
                                   "draft_hash": digest(project["draft"]), "verification": self.verification_signature(summary),
                                   "at": datetime.now(UTC).isoformat()}
            self.event(project, "script_approved", actor, dict(project["approval"]))
            return project

    def upload(self, choice_id, beat, filename, data, actor, provenance, *, role="owner", source=None):
        if role == "owner":
            self.owner(actor)
        elif role != "assistant" or actor != "assistant":
            raise ValueError("Attribution visuelle invalide.")
        self.dossier(choice_id)
        if not provenance.strip() or not data or len(data) > 100 * 1024 * 1024:
            raise ValueError("Visuel manuel : fichier ≤100 Mo et provenance requis.")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}:
            raise ValueError("Visuels : JPEG, PNG, WebP, MP4 ou MOV.")
        with self.transaction(choice_id) as project:
            if type(beat) is not int or not 0 <= beat < 48:
                raise ValueError("Index de photo invalide (0 à 47).")
            sha = hashlib.sha256(data).hexdigest()
            relative = self.path(choice_id, f"assets/{sha}{suffix}")
            target = self.store.path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temp = target.with_suffix(suffix + ".tmp")
                temp.write_bytes(data)
                temp.replace(target)
            probe = self.probe(target)
            videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
            if not videos or min(videos[0]["width"], videos[0]["height"]) < 720:
                raise ValueError("Visuel trop petit ou invalide : dimension courte ≥720 pixels.")
            if suffix in {".mp4", ".mov"}:
                duration = float(probe["format"]["duration"])
                source = dict(source or {})
                start, end = float(source.get("clip_in", 0)), float(source.get("clip_out", duration))
                if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
                    raise ValueError("Extrait vidéo hors de la durée du fichier.")
                source.update(clip_in=start, clip_out=end)
            asset = Asset(project["content"]["id"], AssetType.IMAGE if suffix not in {".mp4", ".mov"} else AssetType.VIDEO,
                          relative, metadata={"sha256": sha, "filename": Path(filename).name, "provenance": provenance,
                                              "manual": role == "owner", "beat": beat, "probe": probe, "actor": actor,
                                              "source": source, "owner_verified": False})
            project["assets"][str(beat)] = asset.to_dict()
            project["visual_lot_approval"] = None
            self.event(project, "manual_visual_added" if role == "owner" else "assistant_visual_selected", actor, asset.to_dict())
            return project

    def set_visual_plan(self, choice_id, slots, note, *, clear_beats=()):
        """Assistant storyboard; missing shots stay missing, previous assets stay in history."""
        self.dossier(choice_id, allow_historical=True)
        with self.transaction(choice_id) as project:
            if not note.strip() or len(slots) != len(project["draft"]["beats"]):
                raise ValueError("Plan motivé avec un emplacement par segment requis.")
            for slot in slots:
                if slot.get("kind") not in {"IMAGE", "VIDEO", "EITHER"} or not slot.get("subject", "").strip():
                    raise ValueError("Type et sujet du plan requis.")
            removed = {str(i): project["assets"].pop(str(i)) for i in clear_beats if str(i) in project["assets"]}
            from app.services.reel_editing import shot_sequence, visual_assets
            pool=visual_assets(dict(project,visual_plan=slots))
            for i,slot in enumerate(slots):shot_sequence(slot,i,len(pool))
            project["visual_plan"] = slots
            project["visual_lot_approval"] = None
            self.event(project, "visual_plan_proposed", "assistant", {"slots": slots, "note": note, "previous_assets": removed})
            return project

    def visual_lot(self, project):
        slots, gaps = [], []
        plan = project.get("visual_plan", [])
        if plan and len(plan) != len(project["draft"]["beats"]):
            gaps.append("Plan visuel à réviser après modification du nombre de segments.")
        from app.services.reel_editing import visual_assets, shot_sequence
        from app.services.reel_rhythm import audit_plan
        pool=visual_assets(project)
        for i, beat in enumerate(project["draft"]["beats"]):
            target = plan[i] if i < len(plan) else {"kind": "EITHER", "subject": "Visuel réel correspondant au segment"}
            for shot in shot_sequence(target,i,len(pool)):
                index=shot.get('asset_index',shot.get('asset_beat'))
                asset=pool[index]
                quality=self.source_quality(asset,project) if asset else None
                reason=None
                if not asset:
                    reason="Photo différente à sourcer et inspecter par l’assistant."
                elif target.get("kind","EITHER")!="EITHER" and asset["type"]!=target["kind"]:
                    reason="Type attendu : "+target["kind"]
                elif quality and not quality["passed"]:
                    reason=quality["reason"]
                if reason:gaps.append(f"Plan {len(slots)+1} : {reason}")
                slots.append({"beat":i,"shot_index":len(slots),"asset_index":index,"target":target,"asset":asset,"text":beat["text"],"gap":reason,"source_quality":quality,
                              "estimated_seconds":round(shot['end_seconds']-shot['start_seconds'] if 'start_seconds' in shot else max(2,len(beat["text"].split())/2.5)*shot.get('weight',1)/sum(x.get('weight',1) for x in shot_sequence(target,i,len(pool))),1)})
        rhythm=audit_plan(project)
        gaps.extend(rhythm['errors'])
        from app.services.visual_quality import audit, VERSION
        try:
            visual_quality = audit(self, project)
            gaps.extend(visual_quality["errors"])
        except ValueError as error:
            visual_quality = {"passed": False, "errors": [str(error)]}
            gaps.append(str(error))
        signature = digest({"draft": project["draft"], "plan": plan, "assets": project["assets"],
                            "visual_policy": VERSION, "reviews": project.get("visual_reviews", {}),
                            "photo_motion": project.get("photo_motion", self.config.get("photo_motion"))})
        review = project.get("visual_lot_approval")
        return {"slots": slots, "gaps": gaps, "complete": not gaps, "signature": signature, "visual_quality": visual_quality, "rhythm": rhythm,
                "approved": bool(review and review["passed"] and review["signature"] == signature),
                "photos": len({s["asset"]["metadata"]["sha256"] for s in slots if s["asset"] and s["asset"]["type"] == "IMAGE"}),
                "videos": len({s["asset"]["metadata"]["sha256"] for s in slots if s["asset"] and s["asset"]["type"] == "VIDEO"})}

    def source_quality(self, asset, project):
        """Native central crop budget, distinct from the encoded video's size."""
        from app.services.visual_quality import native_quality
        from app.services.visual_presentation import presentation
        stream = next((s for s in asset["metadata"].get("probe", {}).get("streams", []) if s.get("codec_type") == "video"), {})
        quality = native_quality(stream.get("width", 0), stream.get("height", 0), self.config,
                                 project.get("photo_motion") if asset.get("type", "IMAGE") == "IMAGE" else {}, presentation=presentation(asset))
        source = asset["metadata"].get("source") or {}
        inspection = project.get("visual_reviews", {}).get(source.get("id") or asset["metadata"].get("sha256"), {})
        sharp = inspection.get("sharpness") is True and inspection.get("decision") == "accepted"
        quality.update(sharpness_reviewed=sharp, passed=quality["passed"] and sharp)
        if quality["resolution_passed"] and not sharp:
            quality["reason"] = "Netteté et correspondance au lieu du fichier exact à inspecter par l’assistant."
        return quality

    def review_visual_lot(self, choice_id, actor, note, passed, signature):
        self.owner(actor)
        self.dossier(choice_id)
        with self.transaction(choice_id) as project:
            lot = self.visual_lot(project)
            if type(passed) is not bool or not note.strip() or signature != lot["signature"]:
                raise ValueError("Revue explicite du lot affiché requise ; actualisez s’il a changé.")
            if passed and not lot["complete"]:
                raise ValueError("Le lot comporte encore des plans manquants.")
            review = {"actor": actor, "note": note, "passed": passed, "signature": signature, "at": datetime.now(UTC).isoformat()}
            project["visual_lot_approval"] = review
            self.event(project, "visual_lot_reviewed", actor, review)
            return project

    def package_visual_lot(self, choice_id):
        """Review package of exact ingredients, never a ready-to-publish Reel."""
        self.dossier(choice_id)
        with self.transaction(choice_id) as project:
            lot = self.visual_lot(project)
            for slot in lot["slots"]:
                asset = slot["asset"]
                if not asset:
                    continue
                if file_hash(self.store.path(asset["file_path"])) != asset["metadata"]["sha256"]:
                    raise ValueError("Média modifié : lot à réviser.")
                source = asset["metadata"].get("source")
                if source and source.get("source_id") and file_hash(self.store.path(f"sources/{source['source_id']}.json")) != source["evidence_sha256"]:
                    raise ValueError("Preuve visuelle modifiée.")
            previous = next((v for v in project.get("visual_packages", []) if v["signature"] == lot["signature"] and v.get("schema_version") == 2), None)
            if previous:
                if file_hash(self.store.path(previous["file_path"])) != previous["sha256"]:
                    raise ValueError("Archive du lot modifiée.")
                return previous
            relative = self.path(choice_id, f"visual-lots/{lot['signature']}-v2.zip")
            target = self.store.path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            manifest = {"purpose": "editorial_media_review_only", "choice": choice_id, "place": project["content"]["title"],
                        "lot": lot, "notice": project.get("visual_lot_notice"), "owner_approved": lot["approved"], "files": []}
            temporary = target.with_suffix('.zip.tmp')
            included_proofs = set()
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for slot in lot["slots"]:
                    asset = slot["asset"]
                    if not asset:
                        continue
                    path = self.store.path(asset["file_path"])
                    if file_hash(path) != asset["metadata"]["sha256"]:
                        raise ValueError("Média modifié : lot à réviser avant téléchargement.")
                    name = f"plan-{slot['beat']+1:02}{path.suffix}"
                    archive.write(path, name)
                    manifest["files"].append({"name": name, "sha256": file_hash(path)})
                    source = asset["metadata"].get("source")
                    if source and source.get("source_id"):
                        proof = self.store.path(f"sources/{source['source_id']}.json")
                        if file_hash(proof) != source["evidence_sha256"]:
                            raise ValueError("Preuve visuelle modifiée.")
                        if source['source_id'] not in included_proofs:
                            archive.write(proof, f"sources/{source['source_id']}.json")
                            included_proofs.add(source['source_id'])
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                archive.writestr('LIRE.txt', 'Lot de médias à vérifier éditorialement. Ce ZIP ne contient pas un Reel produit ou approuvé.\nLes fichiers de chaque plan sont entiers et inchangés ; provenance, limites et découpage dans manifest.json.\n')
            temporary.replace(target)
            receipt = {"file_path": relative, "sha256": file_hash(target), "signature": lot["signature"], "schema_version": 2, "at": datetime.now(UTC).isoformat()}
            if not any(v["signature"] == receipt["signature"] and v.get("schema_version") == 2 for v in project.get("visual_packages", [])):
                project.setdefault("visual_packages", []).append(receipt)
                self.event(project, "visual_lot_packaged", "assistant", receipt)
            return receipt

    def probe(self, path):
        return json.loads(subprocess.run([self.config["ffprobe"], "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                         capture_output=True, text=True, check=True, timeout=60).stdout)

    def config_path(self, key):
        path = Path(self.config[key]).expanduser()
        return path if path.is_absolute() else self.repository / path

    def preflight(self):
        paths = {k: str(self.config_path(k)) for k in ("voice_python", "voice_checkpoint", "voice_reference", "font")}
        missing = [k for k, value in paths.items() if not Path(value).exists()]
        for filename in ("ve.pt", "s3gen.pt", "t3_mtl23ls_v3.safetensors", "grapheme_mtl_merged_expanded_v1.json"):
            if not (Path(paths["voice_checkpoint"]) / filename).exists():
                missing.append(filename)
        for key in ("ffmpeg", "ffprobe"):
            if not shutil.which(self.config[key]):
                missing.append(key)
        if self.config.get("narration", {}).get("mode") == "continuous":
            if not shutil.which("whisper-cli"):
                missing.append("whisper-cli")
            if not self.config_path("alignment_model").is_file():
                missing.append("alignment_model")
        return {"ready": not missing, "missing": missing, "paths": paths, "voice": "Chatterbox multilingual v3 / CPU / offline"}

    def set_caption_timeline(self, choice_id, items, note):
        from app.services.reel_editing import explicit_caption_timeline
        if not isinstance(note, str) or len(note.strip()) < 15:
            raise ValueError('Expliquer la correction des sous-titres.')
        with self.transaction(choice_id) as project:
            # The active revision can carry a newer draft than the historical
            # project draft. Use the same resolved inputs as the renderer.
            active_draft = self.media_inputs(project, allow_historical=True)['draft']
            checked = explicit_caption_timeline(items, active_draft['beats'])
            project['caption_timeline_proposal'] = checked
            self.event(project, 'caption_timeline_proposed', 'assistant', {'items': checked, 'note': note})
            return checked

    def production_inputs(self, project):
        if project.get("studio_review_id"):
            raise ValueError("Validation éditoriale uniquement : aucune autorisation de publication.")
        dossier, summary = self.dossier(project["id"])
        approval = project["approval"]
        if not approval or approval["draft_hash"] != digest(project["draft"]):
            raise ValueError("Approbation humaine de la version actuelle requise.")
        if not summary["scoped_checks_complete"] or approval["verification"] != self.verification_signature(summary):
            raise ValueError("Vérifications modifiées ou ouvertes : nouvelle approbation requise.")
        for point in project["draft"]["used_points"]:
            if summary["latest_checks"][point]["result"] != "verified":
                raise ValueError("Affirmation utilisée sans vérification.")
        from app.services.reel_editing import visual_assets
        assets = visual_assets(project)
        if any(not asset for asset in assets):
            raise ValueError("Sélection assistant incomplète : un média réel par plan est requis.")
        lot = self.visual_lot(project)
        if not lot["complete"] or not lot["approved"]:
            raise ValueError("Lot complet de photos/vidéos à vérifier éditorialement avant production.")
        return {**self.media_inputs(project), "approval": approval, "visual_lot_approval": project["visual_lot_approval"]}

    def media_inputs(self, project, *, allow_historical=False):
        self.dossier(project["id"], allow_historical=allow_historical)
        from app.services.visual_reuse import authorization
        reuse = authorization(self, project, require_latest=True)
        lot = self.visual_lot(project)
        if not lot["complete"]:
            raise ValueError("Sourcing visuel à compléter : " + " ".join(lot["gaps"]))
        from app.services.reel_editing import visual_assets
        assets = visual_assets(project)
        for asset in assets:
            if asset is None:continue  # Unused indices can be gaps; selected shots are checked above.
            if file_hash(self.store.path(asset["file_path"])) != asset["metadata"]["sha256"]:
                raise ValueError("Visuel modifié ; le fournir à nouveau.")
            source = asset["metadata"].get("source")
            if source and source.get("source_id"):
                if project.get("visual_reviews", {}).get(source["id"], {}).get("decision") != "accepted":
                    raise ValueError("Un visuel sélectionné a été écarté ou n'est pas inspecté.")
                proof = self.store.path(f"sources/{source['source_id']}.json")
                if source.get("evidence_sha256") != file_hash(proof):
                    raise ValueError("Preuve du visuel modifiée ; revoir la sélection.")
        dependencies = {k: dependency_hash(self.config_path(k)) for k in ("voice_reference", "font")}
        dependencies["worker"] = dependency_hash(self.repository / "app/services/reel_worker.py")
        dependencies["captions"] = dependency_hash(self.repository / "app/services/reel_captions.py")
        dependencies["editing"] = dependency_hash(self.repository / "app/services/reel_editing.py")
        dependencies["presentation"] = dependency_hash(self.repository / "app/services/visual_presentation.py")
        dependencies["audio_qa"] = dependency_hash(self.repository / "app/services/reel_audio.py")
        dependencies["renderer"] = dependency_hash(Path(__file__))
        dependencies["revision_active"] = dependency_hash(self.repository / "app/services/revision_active.py")
        if project.get('revision_mix_proposal'):
            dependencies["revision_mix"] = dependency_hash(self.repository / "app/services/reel_revision_mix.py")
        if project.get('revision_story_proposal'):
            dependencies["revision_story"] = dependency_hash(self.repository / "app/services/reel_revision_story.py")
        if project.get('revision_voice_proposal'):
            dependencies["revision_voice"] = dependency_hash(self.repository / "app/services/reel_revision_voice.py")
        dependencies["visual_quality"] = dependency_hash(self.repository / "app/services/visual_quality.py")
        dependencies["visual_reuse"] = dependency_hash(self.repository / "app/services/visual_reuse.py")
        dependencies["rhythm"] = dependency_hash(self.repository / "app/services/reel_rhythm.py")
        if self.config.get("narration", {}).get("mode") == "continuous":
            dependencies["narration_worker"] = dependency_hash(self.repository / "app/services/reel_narration.py")
            dependencies["narration_alignment"] = dependency_hash(self.repository / "app/services/reel_alignment.py")
            dependencies["approved_audio_alignment"] = dependency_hash(self.repository / "app/services/reel_approved_audio.py")
            dependencies["alignment_model"] = dependency_hash(self.config_path("alignment_model"))
        for name in ("ve.pt", "s3gen.pt", "t3_mtl23ls_v3.safetensors", "grapheme_mtl_merged_expanded_v1.json"):
            dependencies[name] = dependency_hash(self.config_path("voice_checkpoint") / name)
        candidate = None
        if any(project.get(k) for k in ('revision_mix_proposal','revision_voice_proposal','revision_story_proposal')):
            from app.services.editorial_inbox import EditorialInbox
            from app.services.revision_active import active_revision
            reviews = [r for r in EditorialInbox(self.editorial, self).list() if r['place_id'] == project['place_id']]
            if not reviews:
                raise ValueError('Retour de correction introuvable.')
            active = active_revision(project, reviews[-1])
            if active == 'revision_mix_proposal':
                from app.services.reel_revision_mix import mix_candidate
                candidate = mix_candidate(self, project, reviews[-1])
            elif active == 'revision_voice_proposal':
                from app.services.reel_revision_voice import removed_beat_candidate
                candidate = removed_beat_candidate(self, project, reviews[-1])
            elif active == 'revision_story_proposal':
                from app.services.reel_revision_story import story_candidate
                candidate = story_candidate(self, project, reviews[-1])
        return {"draft": candidate['draft'] if candidate else project["draft"], "assets": assets, "visual_plan": candidate['visual_plan'] if candidate else project.get("visual_plan", []), "config": self.config, "dependencies": dependencies, **({"caption_timeline_proposal": project['caption_timeline_proposal']} if project.get('caption_timeline_proposal') else {}), **({"photo_motion": project["photo_motion"]} if "photo_motion" in project else {}), **({"photo_reuse_review_id": reuse['review_id']} if reuse else {}), **({"approved_narration": candidate['narration'] if candidate else project["approved_narration"]} if project.get("approved_narration") else {})}

    def preview_inputs(self, project):
        return {**self.media_inputs(project, allow_historical=True), "mode": "preview", "notice": "VERSION DE TRAVAIL · FAITS NON VALIDÉS"}

    def start_preview(self, choice_id, actor, instruction):
        self.owner(actor)
        if not instruction.strip():
            raise ValueError("Demande explicite de maquette requise.")
        return self.start_run(choice_id, preview_request={"actor": actor, "instruction": instruction})

    def start_run(self, choice_id, *, preview_request=None):
        from app.services.revision_progress import require_plan
        require_plan(self.editorial)
        with self.transaction(choice_id) as project:
            inputs = self.preview_inputs(project) if preview_request else self.production_inputs(project)
            preflight = self.preflight()
            if not preflight["ready"]:
                raise ValueError("Production locale indisponible : " + ", ".join(preflight["missing"]))
            if any(r["status"] in {"queued", "running"} for r in project["runs"]):
                raise ValueError("Production déjà en cours.")
            key = digest(inputs)
            for run in reversed(project["runs"]):
                if run["input_hash"] == key and run["status"] == "rendered" and file_hash(self.store.path(run["video"])) == run["sha256"]:
                    return run
            run = {"id": uuid4().hex, "created_at": datetime.now(UTC).isoformat(), "input_hash": key, "status": "queued", "stage": "queued",
                   "inputs": inputs, "qa": None, "technical": None, "error": None,
                   "mode": "preview" if preview_request else "production", "preview_request": preview_request}
            project["runs"].append(run)
            if not preview_request:
                content = content_from(project["content"])
                content.status = ContentStatus.APPROVED
                content.start_media_production()
                project["content"] = content.to_dict()
            self.event(project, "preview_requested" if preview_request else "production_requested",
                       preview_request["actor"] if preview_request else "workflow",
                       {"run_id": run["id"], "input_hash": key, "request": preview_request, "facts_validated": False if preview_request else True})
            return run

    def update_run(self, choice_id, run_id, **values):
        with self.transaction(choice_id) as project:
            run = next(r for r in project["runs"] if r["id"] == run_id)
            run.update(values)
            return run

    def run_command(self, args, logfile, timeout=600):
        with logfile.open("a") as log:
            subprocess.run(args, stdout=log, stderr=log, check=True, timeout=timeout,
                           env={**__import__("os").environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})

    def render(self, choice_id, run_id):
        run = next(r for r in self.read(choice_id)["runs"] if r["id"] == run_id)
        folder = self.store.path(self.path(choice_id, f"runs/{run_id}/manifest.json")).parent
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "production.log"
        try:
            input_reader = self.preview_inputs if run.get("mode") == "preview" else self.production_inputs
            current = input_reader(self.read(choice_id))
            if digest(current) != run["input_hash"]:
                raise ValueError("Entrées modifiées avant montage : préparer un nouveau rendu.")
            from app.services.visual_quality import audit, require
            visual_project = dict(self.read(choice_id), draft=run["inputs"]["draft"],
                                  assets={str(i): a for i, a in enumerate(run["inputs"]["assets"])},
                                  visual_plan=run["inputs"].get("visual_plan", []),
                                  photo_reuse_review_id=run["inputs"].get("photo_reuse_review_id"),
                                  photo_motion=run["inputs"].get("photo_motion", self.config.get("photo_motion", {})))
            visual_quality = require(audit(self, visual_project, deep=True))
            self.update_run(choice_id, run_id, visual_quality=visual_quality, status="running", stage="voice")
            paths = self.preflight()["paths"]
            worker = str(self.repository / "app/services/reel_worker.py")
            beats = run["inputs"]["draft"]["beats"]
            continuous = self.config.get("narration", {}).get("mode") == "continuous"
            if not continuous and any(slot.get('shots') for slot in run['inputs'].get('visual_plan',[])):
                raise ValueError('Le découpage indépendant des images demande une narration continue.')
            if continuous:
                from app.services.reel_alignment import prepare
                narration, boundaries = prepare(self, run["inputs"], folder, paths, log)
                audio = [None] * len(beats)
                self.update_run(choice_id, run_id, approved_narration=run["inputs"].get("approved_narration"), narration={"mode": "continuous", "settings": self.config["narration"],
                                                           "alignment": str((folder / "alignment.json").relative_to(self.store.root))})
            else:
                # Voice cache independent of approval/job id: unchanged phrases are reusable.
                identity = {"checkpoint": paths["voice_checkpoint"], "model": file_hash(Path(paths["voice_checkpoint"]) / "t3_mtl23ls_v3.safetensors"),
                            "reference": file_hash(paths["voice_reference"]), "worker": file_hash(worker), "settings": "v3_cpu_fr_defaults_text_hash_seed_v1"}
                missing, audio = [], []
                for beat in beats:
                    key = digest({"identity": identity, "text": beat["text"]})
                    path = self.store.path(f"voice-cache/{key}.wav")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    receipt = self.store.read(f"voice-cache/{key}.json")
                    if not receipt or receipt.get("identity") != identity or receipt.get("text") != beat["text"] or not path.exists() or file_hash(path) != receipt["sha256"]:
                        missing.append({"text": beat["text"], "output": str(path), "key": key, "seed": int(key[:8],16) % (2**31)})
                    audio.append(path)
                manifest = folder / "voice.json"
                manifest.write_text(json.dumps({"checkpoint": paths["voice_checkpoint"], "reference": paths["voice_reference"], "identity": identity, "items": missing}))
                if missing:
                    self.run_command([paths["voice_python"], worker, "voice", str(manifest)], log, timeout=3600)
                    for item in missing:
                        self.store.write(f"voice-cache/{item['key']}.json", {"sha256": file_hash(item["output"]), "identity": identity, "text": item["text"], "seed": item["seed"]})
            self.update_run(choice_id, run_id, stage="subtitles")
            caption_report = json.loads((folder / 'caption-timeline.json').read_text()) if continuous and (folder / 'caption-timeline.json').exists() else None
            caption_items = caption_report['items'] if caption_report else [
                {'text': b['text'], 'start': boundaries[i], 'end': boundaries[i+1]} for i, b in enumerate(beats)
            ] if continuous else [{'text': b['text']} for b in beats]
            caption_items = [{**item, 'output': str(folder / f'caption-{i}.png')} for i, item in enumerate(caption_items)]
            captions = folder / "captions.json"
            captions.write_text(json.dumps({"font": paths["font"], "name": self.read(choice_id)["content"]["title"],
                                           "preview": run.get("mode") == "preview",
                                           "items": caption_items}))
            self.run_command([paths["voice_python"], str(self.repository / "app/services/reel_captions.py"), "caption", str(captions)], log)
            self.update_run(choice_id, run_id, stage="assembly")
            duration, timeline, parts = 0.0, [], []
            if continuous:
                from app.services.reel_editing import picture_timeline, has_explicit_picture_times
                shots=picture_timeline(beats,run['inputs']['assets'],boundaries,run['inputs'].get('visual_plan'))
                music=run['inputs'].get('approved_narration',{}).get('music')
                if music:
                    from app.services.reel_music import snap_music_cuts, selected_mix
                    if not has_explicit_picture_times(run['inputs'].get('visual_plan')):
                        shots=snap_music_cuts(shots,music.get('pulse_times',[]))
                    narration=selected_mix(self,run['inputs']['approved_narration'],float(self.probe(narration)['format']['duration']))
                    pulse_frames={round(t*30) for t in music.get('pulse_times',[])
                                  if type(t) in (int,float) and math.isfinite(t) and t>=0}
                    synced=bool(shots[1:]) and all(round(s['start']*30) in pulse_frames for s in shots[1:])
                    self.update_run(choice_id,run_id,music=dict(music,integrated=True,photo_sync=synced))
            else:
                shots=[{'beat_index':i,'asset_index':i} for i in range(len(beats))]
            if continuous:
                from app.services.reel_rhythm import verify_timeline
                verify_timeline(self.read(choice_id),shots)
            for index, shot in enumerate(shots):
                beat=beats[shot['beat_index']]
                voice=audio[shot['beat_index']]
                asset=run['inputs']['assets'][shot['asset_index']]
                # Frame-rounded beat timing; exact voice per beat gives honest subtitle timing.
                seconds = (shot['end']-shot['start']) if continuous else math.ceil((float(self.probe(voice)["format"]["duration"]) + 0.20) * 30) / 30
                video = self.store.path(asset["file_path"])
                part = folder / f"part-{index}.mp4"
                arguments = [self.config["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y"]
                if asset["type"] == "IMAGE":
                    arguments += ["-loop", "1"]
                else:
                    clip = asset["metadata"]["source"]
                    if seconds > clip["clip_out"] - clip["clip_in"] + 1e-6:
                        raise ValueError(f"Plan {index+1} : extrait vidéo trop court pour la voix ({seconds:.1f} s). Choisir un extrait plus long ; voix en cache conservée.")
                    arguments += ["-ss", str(clip["clip_in"])]
                from app.services.reel_editing import photo_filter, overlay_filter
                from app.services.visual_presentation import asset_filter
                frame_filter = asset_filter(asset, seconds, run['inputs'].get('photo_motion', self.config.get('photo_motion')))
                visual_filter = '[0:v]' + frame_filter + '[v];'
                arguments += ["-i", str(video)]
                if continuous:
                    visible, overlays = overlay_filter(caption_items, duration, duration + seconds)
                    for item in visible:
                        arguments += ['-loop', '1', '-i', item['output']]
                    arguments += ["-filter_complex", visual_filter + overlays, "-map", "[out]", "-an"]
                else:
                    arguments += ["-i", str(voice), "-loop", "1", "-i", str(folder / f"caption-{index}.png"),
                                  "-filter_complex", visual_filter + "[v][2:v]overlay=0:0:format=auto,format=yuv420p[out];[1:a]apad[a]",
                                  "-map", "[out]", "-map", "[a]"]
                arguments += ["-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                              "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
                if not continuous:
                    arguments += ["-c:a", "aac", "-ar", "48000", "-ac", "2"]
                arguments += ["-movflags", "+faststart", str(part)]
                self.run_command(arguments, log)
                parts.append(part)
                timeline.append({"start": duration, "end": duration + seconds, "text": beat["text"], "asset": asset["file_path"],
                                 "clip": asset.get("metadata", {}).get("source", {}), "beat_index": shot["beat_index"], "asset_index": shot["asset_index"], "visual_filter": frame_filter})
                duration += seconds
            listing = folder / "concat.txt"
            listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
            final = folder / "reel.mp4"
            arguments = [self.config["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing)]
            if continuous:
                # One intact PCM track muxed once: cuts never restart, pad or splice speech.
                arguments += ["-i", str(narration), "-map", "0:v:0", "-map", "1:a:0"]
            arguments += ["-c:v", "copy"]
            # A prepared music mix must not be dynamically normalized again:
            # its gain would vary with speech and undo the parallel-track mix.
            if not (continuous and music):
                arguments += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=summary"]
            arguments += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(final)]
            self.run_command(arguments, log)
            def timestamp(value):
                ms = round(value * 1000)
                return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"
            (folder / "subtitles.srt").write_text("\n".join(f"{i+1}\n{timestamp(b['start'])} --> {timestamp(b['end'])}\n{b['text']}\n" for i, b in enumerate(caption_items if continuous else timeline)))
            (folder / "caption.txt").write_text(run["inputs"]["draft"]["caption"] + "\n")
            self.update_run(choice_id, run_id, stage="technical_qa")
            technical = self.technical_qa(final, duration, log)
            # Inputs could have changed during the slow CPU production.
            current = input_reader(self.read(choice_id))
            if digest(current) != run["input_hash"]:
                raise ValueError("Dossier/texte/visuels modifiés pendant le montage : rendu historique, à relancer.")
            from app.services.visual_quality import verify_run
            visual_quality = verify_run(self, self.read(choice_id), dict(run, timeline=timeline, technical=technical), video=final)
            (folder / "visual-quality.json").write_text(json.dumps(visual_quality, ensure_ascii=False, indent=2))
            files = {name: file_hash(folder / name) for name in ("reel.mp4", "caption.txt", "subtitles.srt")}
            self.update_run(choice_id, run_id, status="rendered", rendered_at=datetime.now(UTC).isoformat(), watermark_free=True, stage="awaiting_preview_review" if run.get("mode") == "preview" else "awaiting_human_qa", technical=technical, visual_quality=visual_quality, files=files,
                            video=str(final.relative_to(self.store.root)), sha256=file_hash(final), timeline=timeline)
        except Exception as error:
            self.update_run(choice_id, run_id, status="failed", stage="failed", error=str(error), log=str(log.relative_to(self.store.root)))

    def technical_qa(self, video, expected_duration, log):
        probe = self.probe(video)
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        a = next((s for s in probe["streams"] if s["codec_type"] == "audio"), {})
        duration = float(probe["format"]["duration"])
        checks = {"geometry": (v["width"], v["height"]) == (1080, 1920), "video_codec": v["codec_name"] == "h264",
                  "audio_codec": a.get("codec_name") == "aac", "pixel_format": v["pix_fmt"] == "yuv420p",
                  "fps": v["r_frame_rate"] in {"30/1", "60/2"},
                  "duration": self.config["min_duration"] <= duration <= self.config["max_duration"],
                  "timeline": abs(duration - expected_duration) < 0.25}
        self.run_command([self.config["ffmpeg"], "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-"], log)
        checks["full_decode"] = True
        from app.services.reel_audio import measure_audio
        audio = measure_audio(video, ffmpeg=self.config["ffmpeg"]) if a else None
        checks["audio_levels"] = audio is not None and audio["passed"]
        return {"passed": all(checks.values()), "checks": checks, "duration": duration, "probe": probe, "audio": audio,
                "notice": "Contrôle technique ; qualité perceptuelle et faits à revoir humainement."}

    def human_qa(self, choice_id, run_id, actor, checks, note, passed):
        self.owner(actor)
        if next(r for r in self.read(choice_id)["runs"] if r["id"] == run_id).get("mode") == "preview":
            raise ValueError("Maquette de travail : effectuer les vérifications et accords, puis produire le rendu de publication.")
        if not note.strip() or set(checks) != set(QA_POINTS) or not all(isinstance(v, bool) for v in checks.values()):
            raise ValueError("Revue explicite des six points et note requises.")
        with self.transaction(choice_id) as project:
            inputs = self.production_inputs(project)
            run = next(r for r in project["runs"] if r["id"] == run_id)
            if run["status"] != "rendered" or digest(inputs) != run["input_hash"]:
                raise ValueError("Rendu absent ou obsolète.")
            if file_hash(self.store.path(run["video"])) != run["sha256"]:
                raise ValueError("Vidéo modifiée après contrôle technique.")
            self.check_render_files(run)
            if passed and (not all(checks.values()) or not run["technical"]["passed"]):
                raise ValueError("Échec technique ou revue incomplète : corriger avant export.")
            run["qa"] = {"actor": actor, "passed": passed, "checks": checks, "note": note,
                         "at": datetime.now(UTC).isoformat(), "video_sha256": run["sha256"]}
            content = content_from(project["content"])
            content.qa_information.update(technical_report=run["technical"], human_video_review=run["qa"])
            if passed:
                content.status = ContentStatus.APPROVED
                content.start_media_production()
                content.mark_ready()
            else:
                content.status = ContentStatus.PRODUCING
            project["content"] = content.to_dict()
            self.event(project, "human_video_qa", actor, {"run_id": run_id, **run["qa"]})
            return project

    def export(self, choice_id, run_id, actor):
        self.owner(actor)
        if next(r for r in self.read(choice_id)["runs"] if r["id"] == run_id).get("mode") == "preview":
            raise ValueError("Cette maquette peut être téléchargée pour revue ; aucun export validé pour publication.")
        with self.transaction(choice_id) as project:
            inputs = self.production_inputs(project)
            run = next(r for r in project["runs"] if r["id"] == run_id)
            if run["status"] != "rendered" or digest(inputs) != run["input_hash"] or not run["qa"] or not run["qa"]["passed"]:
                raise ValueError("Contrôle humain positif du rendu actuel requis avant export.")
            if not run["technical"]["passed"] or file_hash(self.store.path(run["video"])) != run["qa"]["video_sha256"]:
                raise ValueError("Intégrité vidéo ou contrôle technique invalide.")
            self.check_render_files(run)
            for item in project["exports"]:
                if item["run_id"] == run_id:
                    return item
            target = self.store.path(self.path(choice_id, f"exports/{run_id}/manifest.json"))
            target.parent.mkdir(parents=True, exist_ok=True)
            source = self.store.path(run["video"]).parent
            for name in ("reel.mp4", "caption.txt", "subtitles.srt"):
                shutil.copyfile(source / name, target.parent / name)
            manifest = {"run_id": run_id, "choice_id": choice_id, "place_id": project["place_id"], "actor": actor,
                        "publication": "manuelle uniquement", "input_hash": run["input_hash"], "approval": project["approval"],
                        "qa": run["qa"], "folder": str(target.parent),
                        "files": {p.name: file_hash(p) for p in target.parent.iterdir() if p.is_file()}}
            self.store.write(str(target.relative_to(self.store.root)), manifest, immutable=True)
            project["exports"].append(manifest)
            self.event(project, "manual_publication_export", actor, manifest)
            return manifest

    def check_render_files(self, run):
        folder = self.store.path(run["video"]).parent
        for name, expected in run["files"].items():
            if file_hash(folder / name) != expected:
                raise ValueError("Fichier rendu modifié : " + name)

    def recover_interrupted(self):
        for path in self.store.root.glob("projects/*/project.json"):
            with self.transaction(path.parent.name) as project:
                if project.get("sourcing_status") in {"queued", "running"}:
                    project["sourcing_status"] = "interrupted"
                for run in project["runs"]:
                    if run["status"] in {"queued", "running"}:
                        run.update(status="interrupted", stage="interrupted", error="Serveur arrêté ; relancer pour réutiliser les phrases vocales en cache.")
