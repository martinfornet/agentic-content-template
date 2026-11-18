"""Small, persistent approval-gated workflow for creator supplied footage."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Union
from uuid import uuid4


STAGES = ("proposed", "footage_approved", "voice_generated", "voice_approved", "assembled", "export_approved")


def _hash_file(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CreatorWorkflow:
    """Persist proposals and exact-version approvals under a local workspace."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str) -> Path:
        if not project_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in project_id):
            raise ValueError("Identifiant de projet invalide.")
        return self.root / f"{project_id}.json"

    def read(self, project_id: str) -> dict:
        path = self._path(project_id)
        if not path.is_file():
            raise ValueError("Projet inconnu.")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, project: dict) -> dict:
        project["updated_at"] = datetime.now(timezone.utc).isoformat()
        target = self._path(project["id"])
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        return project

    @staticmethod
    def _event(project: dict, kind: str, actor: str, details: dict) -> None:
        if not actor or not actor.strip():
            raise ValueError("Le nom de la personne responsable est requis.")
        project["history"].append({"kind": kind, "actor": actor.strip(),
                                   "at": datetime.now(timezone.utc).isoformat(), **details})

    def create_proposal(self, *, idea: str, shooting_brief: str, script: str,
                        actor: str = "model", trend_notes: str = "") -> dict:
        """Store the model's creative proposal; trend notes are optional inspiration only."""
        values = (idea, shooting_brief, script)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError("Idée, consignes de tournage et texte de voix requis.")
        project_id = uuid4().hex
        project = {"schema_version": 1, "id": project_id, "stage": "proposed",
                   "proposal": {"idea": idea.strip(), "shooting_brief": shooting_brief.strip(),
                                "script": script.strip(), "trend_notes": trend_notes.strip()},
                   "footage": None, "voice": None, "assembly": None, "final_review": None,
                   "history": []}
        self._event(project, "proposal_created", actor, {"trend_notes_present": bool(trend_notes.strip())})
        return self._save(project)

    def _advance(self, project_id: str, expected: str, next_stage: str) -> dict:
        project = self.read(project_id)
        if project["stage"] != expected:
            raise ValueError(f"Étape requise : {expected} (étape actuelle : {project['stage']}).")
        project["stage"] = next_stage
        return project

    def approve_footage(self, project_id: str, files: list[str | Path], *, actor: str) -> dict:
        project = self._advance(project_id, "proposed", "footage_approved")
        if not files:
            raise ValueError("Fournir au moins un fichier filmé.")
        recorded = []
        for filename in files:
            path = Path(filename).resolve()
            if not path.is_file():
                raise ValueError(f"Fichier vidéo introuvable : {path}")
            recorded.append({"path": str(path), "sha256": _hash_file(path)})
        project["footage"] = {"files": recorded, "approved_by": actor.strip(),
                              "approved_at": datetime.now(timezone.utc).isoformat()}
        self._event(project, "footage_approved", actor, {"files": recorded})
        return self._save(project)

    def record_voice(self, project_id: str, audio_file: Union[str, Path], *, engine: str = "Chatterbox") -> dict:
        project = self._advance(project_id, "footage_approved", "voice_generated")
        audio = Path(audio_file).resolve()
        if not audio.is_file():
            raise ValueError(f"Audio généré introuvable : {audio}")
        project["voice"] = {"path": str(audio), "sha256": _hash_file(audio), "engine": engine,
                            "script_sha256": hashlib.sha256(project["proposal"]["script"].encode()).hexdigest(),
                            "approved_by": None}
        self._event(project, "voice_generated", "system", {"engine": engine, "sha256": project["voice"]["sha256"]})
        return self._save(project)

    def approve_voice(self, project_id: str, *, actor: str) -> dict:
        project = self._advance(project_id, "voice_generated", "voice_approved")
        voice = project["voice"]
        if _hash_file(voice["path"]) != voice["sha256"]:
            raise ValueError("L’audio a changé depuis sa génération : le faire écouter à nouveau.")
        voice["approved_by"] = actor.strip()
        voice["approved_at"] = datetime.now(timezone.utc).isoformat()
        self._event(project, "voice_approved", actor, {"sha256": voice["sha256"]})
        return self._save(project)

    def record_assembly(self, project_id: str, video_file: Union[str, Path]) -> dict:
        project = self._advance(project_id, "voice_approved", "assembled")
        video = Path(video_file).resolve()
        if not video.is_file():
            raise ValueError(f"Vidéo assemblée introuvable : {video}")
        project["assembly"] = {"path": str(video), "sha256": _hash_file(video),
                               "footage_hashes": [item["sha256"] for item in project["footage"]["files"]],
                               "voice_sha256": project["voice"]["sha256"]}
        self._event(project, "assembly_recorded", "system", {"sha256": project["assembly"]["sha256"]})
        return self._save(project)

    def approve_export(self, project_id: str, *, actor: str, note: str = "") -> dict:
        project = self._advance(project_id, "assembled", "export_approved")
        assembly = project["assembly"]
        if _hash_file(assembly["path"]) != assembly["sha256"]:
            raise ValueError("La vidéo assemblée a changé depuis sa revue.")
        project["final_review"] = {"approved_by": actor.strip(), "note": note.strip(),
                                   "sha256": assembly["sha256"], "approved_at": datetime.now(timezone.utc).isoformat()}
        self._event(project, "export_approved", actor, {"sha256": assembly["sha256"]})
        return self._save(project)
