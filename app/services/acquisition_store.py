"""Atomic local acquisition storage, independent of production Content/Asset."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.models.acquisition import AcquiredContent


def stable_key(platform: str, identity: str) -> str:
    return platform + "-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


class AcquisitionStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise ValueError("Acquisition path must stay inside storage root.")
        return candidate

    def write(self, relative: str, payload, *, immutable=False):
        target = self.path(relative)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if immutable and target.exists():
            if json.loads(target.read_text()) != payload:
                raise ValueError("Immutable evidence cannot be overwritten.")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, relative, default=None):
        path = self.path(relative)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def save_source(self, source):
        self.write(f"sources/{source.id}.json", source.to_dict(), immutable=True)

    def load_content(self, observation_id):
        value = self.read(f"observations/{observation_id}/acquisition.json")
        return AcquiredContent.from_dict(value) if value else None

    def contents(self):
        folder = self.root / "observations"
        return [AcquiredContent.from_dict(json.loads(p.read_text()))
                for p in sorted(folder.glob("*/acquisition.json"))] if folder.exists() else []

    def save_content(self, content: AcquiredContent):
        content.__post_init__()
        observation_id = content.observation.id
        for snapshot in content.observation.performance:
            self.write(f"observations/{observation_id}/snapshots/{snapshot.id}.json",
                       snapshot.to_dict(), immutable=True)
        self.write(f"observations/{observation_id}/acquisition.json", content.to_dict())

    def save_attempt(self, attempt):
        self.write(f"runs/{attempt.run_id}/attempts/{attempt.id}.json", attempt.to_dict())

    def unfinished_attempts(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / "runs").glob("*/attempts/*.json"))
                if json.loads(p.read_text())["status"] in {"pending", "running"}]

    def load_watchlist(self):
        return self.read("watchlist.json", {"schema_version": 1, "entries": {}})["entries"]

    def save_watchlist(self, entries):
        self.write("watchlist.json", {"schema_version": 1, "entries": entries})

    def find_account_key(self, account_id, handle):
        for key, entry in self.load_watchlist().items():
            if account_id and entry.get("platform_account_id") == account_id:
                return key
            if handle and (entry.get("handle") or "").casefold() == handle.casefold() and (
                not account_id or not entry.get("platform_account_id")
            ):
                return key
        return stable_key("instagram", str(account_id or handle))

    def release_media(self, observation_id):
        """Consumer acknowledgement permits retention cleanup; evidence remains."""
        content = self.load_content(observation_id)
        if content is None:
            raise ValueError("Unknown observation.")
        for media in content.media:
            media.protected = False
        self.save_content(content)

    def ready_for_analysis(self, context):
        """Explicit handoff eligibility, not individual-media understanding."""
        ready = []
        for content in self.contents():
            if content.brand_id != context.brand_id or not content.decision_references:
                continue
            decision = self.read(f"decisions/{content.decision_references[-1]}.json")
            if not decision or decision["decision"] != "eligible" or not content.observation.domain_links:
                continue
            try:
                if any(context.is_domain_excluded(link.domain_id) or
                       context.domain_relevance_level(link.domain_id) != link.relevance_level
                       for link in content.observation.domain_links):
                    continue
            except KeyError:
                continue
            if any(m.state.value == "local_ready" and m.local_path and self.path(m.local_path).is_file()
                   for m in content.media):
                ready.append(content)
        return ready
