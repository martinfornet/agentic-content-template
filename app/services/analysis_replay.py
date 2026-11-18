"""Offline replay of retained decomposed spike inputs; never calls a provider."""
import hashlib
import json
from pathlib import Path
import shutil

from app.services.analysis_media import input_parts
from app.services.analysis_io import write_safe
from app.services.video_analyzer import GeminiVideoAnalyzer


class RetainedDecomposedAnalyzer:
    prompt_version = "s02-decomposed-spike-v1"

    def __init__(self, bundle, store, config):
        if config.input_strategy != "frames_audio":
            raise ValueError("Retained decomposed inputs require frames_audio.")
        self.bundle, self.store, self.config = Path(bundle).resolve(), store, config
        self.last_response, self.last_diagnostics = None, {}

    def analyze(self, path, inspection):
        self.last_response, self.last_diagnostics = None, {"method": "sampled_frames_and_audio",
            "response_reused": True, "provider_requests": 0, "original_elapsed_seconds": None,
            "original_prompt_version": self.prompt_version, "stage": "retained_input_validation"}
        folder = self.bundle / inspection["sha256"]
        manifest = json.loads((folder / "manifest.json").read_text())
        if manifest["source_media_sha256"] != inspection["sha256"] or abs(
                manifest["duration_seconds"] - inspection["duration_seconds"]) > .001:
            raise ValueError("Retained input manifest does not match acquired media.")
        if bool(manifest["audio"]) != inspection["has_audio"]:
            raise ValueError("Retained audio availability differs from inspected media.")
        if not manifest["frames"] or len(manifest["frames"]) > 96:
            raise ValueError("Retained frame count is invalid.")
        relative = f"replay/{inspection['sha256']}"
        total = 0
        for item in [*manifest["frames"], *([manifest["audio"]] if manifest["audio"] else [])]:
            source = Path(item["path"]).resolve()
            if not source.is_relative_to(folder) or not source.is_file():
                raise ValueError("Retained input escapes its source bundle or is missing.")
            total += source.stat().st_size
            if total * 4 // 3 > self.config.max_input_bytes:
                raise ValueError("Retained inputs exceed configured payload limit.")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if item.get("sha256", digest) != digest:
                raise ValueError("Retained input checksum mismatch.")
            target = self.store.path(f"{relative}/{source.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            item.update(path=f"{relative}/{source.name}", sha256=digest)
        input_parts(self.store.root, manifest, self.config.max_input_bytes)
        write_safe(self.store, f"{relative}/manifest.json", manifest)
        self.last_diagnostics.update(input_manifest=f"{relative}/manifest.json",
            frame_count=len(manifest["frames"]), original_response_sha256=hashlib.sha256(
                (folder / "response.json").read_bytes()).hexdigest())
        self.last_response = json.loads((folder / "response.json").read_text())
        payload = GeminiVideoAnalyzer._parse_response(self, self.last_response)
        payload["uncertainties"].append(
            "Offline replay of a real retained spike response: current production prompt was not executed.")
        return payload
