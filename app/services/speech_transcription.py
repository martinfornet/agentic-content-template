"""Local Whisper evidence, cached by media, model and decoding settings; no API."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

from app.services.acquisition_store import AcquisitionStore
from app.services.analysis_io import write_safe
from app.services.analysis_media import inspect_media


class LocalSpeechTranscriber:
    def __init__(self, store, model=None, executable="whisper-cli", runner=subprocess.run):
        self.store = store
        self.model = Path(model or os.environ.get("WHISPER_MODEL", "models/whisper/ggml-small.bin"))
        self.executable, self.runner = executable, runner
        self._identity = None

    def identity(self):
        if self._identity is None:
            with self.model.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            version = self.runner([self.executable, "--version"], capture_output=True, text=True, check=True, timeout=10)
            self._identity = {"engine": "whisper.cpp", "version": version.stdout.strip(),
                              "model_sha256": digest, "language": "auto", "translate": False,
                              "temperature": 0, "fallback": False, "max_context": 0, "schema_version": 1}
        return self._identity

    def transcribe(self, video, inspection):
        identity = self.identity()
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        folder = f"speech/{inspection['sha256']}/{fingerprint}"
        relative = folder + "/transcript.json"
        cached = self.store.read(relative)
        if cached:
            for item in cached["artifacts"]:
                if hashlib.sha256(self.store.path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("Cached speech artifact checksum mismatch.")
            return cached, relative, True
        started = time.monotonic()
        result = {"source_media_sha256": inspection["sha256"], "provenance": identity,
                  "language": None, "segments": [], "artifacts": [], "api_cost_usd": 0}
        target = self.store.path(folder + "/audio.wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        if inspection["has_audio"]:
            self.runner(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                         "-c:a", "pcm_s16le", "-y", str(target)], capture_output=True, check=True, timeout=60)
            output = self.store.path(folder + "/whisper")
            self.runner([self.executable, "-m", str(self.model), "-f", str(target), "-l", "auto",
                         "-oj", "-of", str(output), "-tp", "0", "-nf", "-mc", "0"],
                        capture_output=True, check=True, timeout=600)
            raw_path = folder + "/whisper.json"
            raw = self.store.read(raw_path)
            result["language"] = raw["result"]["language"]
            for entry in raw["transcription"]:
                text = entry["text"].strip()
                # Whisper's non-speech event annotations are not spoken words.
                if not text or (text.startswith("[") and text.endswith("]")) or (text.startswith("(") and text.endswith(")")):
                    continue
                start, end = entry["offsets"]["from"] / 1000, entry["offsets"]["to"] / 1000
                if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start <= end <= inspection["duration_seconds"] + .1:
                    raise ValueError("Invalid local transcript timestamps.")
                result["segments"].append({"id": f"speech-{len(result['segments']):03d}", "kind": "speech",
                    "start_seconds": start, "end_seconds": min(end, inspection["duration_seconds"]), "text": text})
            for artifact in (folder + "/audio.wav", raw_path):
                result["artifacts"].append({"path": artifact, "sha256": hashlib.sha256(self.store.path(artifact).read_bytes()).hexdigest()})
        result["speech_status"] = ("observed" if result["segments"] else "not_observed") if inspection["has_audio"] else "unavailable"
        result["elapsed_seconds"] = time.monotonic() - started
        write_safe(self.store, relative, result)
        return result, relative, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--output", required=True)
    parser.add_argument("--model")
    args = parser.parse_args()
    path = Path(args.video)
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    store = AcquisitionStore(args.output)
    result, relative, cached = LocalSpeechTranscriber(store, args.model).transcribe(path, inspect_media(path, checksum))
    print(json.dumps({"transcript_path": str(store.path(relative)), "cached": cached, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
