"""Analyze one acquired item, preserving immutable acquisition evidence and metrics."""
from dataclasses import asdict, dataclass
import hashlib
import json
import subprocess
import time

from app.models.acquisition import now, new_id, AcquisitionFailure
from app.models.observation_analysis import (
    ObservationAnalysis, MediaEvidence, CreativeFeature, VOCABULARY, SCHEMA_VERSION, PROMPT_VERSION,
)
from app.models.source import Source, SourceType
from app.models.trend import ContentObservation, PatternDimensions
from app.services.analysis_media import inspect_media, extract_frame
from app.services.video_analyzer import model_usage
from app.services.analysis_io import redact_text, write_safe


@dataclass(slots=True)
class AnalyzedObservation:
    observation: ContentObservation
    analysis: ObservationAnalysis


def project_observation(content, analysis, analysis_source):
    """Overlay media understanding on today's acquisition record, never freeze metrics."""
    # Existing acquisition contract owns nested reconstruction; clone via that contract.
    from app.models.acquisition import AcquiredContent
    observation = AcquiredContent.from_dict(content.to_dict()).observation
    observation.source_references = sorted(set(observation.source_references + [analysis_source]))
    def extracted(kind):
        items = sorted((e for e in analysis.evidence if e.kind == kind), key=lambda e: e.start_seconds)
        return "\n".join(e.text for e in items) or None
    observation.transcript = extracted("speech")
    hook_refs = set(analysis.features["hook"].evidence_refs)
    opening = [e for e in analysis.evidence if e.id in hook_refs and e.kind in {"speech", "on_screen_text"} and e.start_seconds < 5]
    observation.observed_hook = "\n".join(e.text for e in sorted(opening, key=lambda e: e.start_seconds)) or None
    observation.characteristics = PatternDimensions(**{dimension:
        (feature.category + (": " + feature.description if feature.description else "")) if feature.category != "unknown" else None
        for dimension, feature in analysis.features.items() if dimension in PatternDimensions.__dataclass_fields__})
    observation.uncertainty_notes = sorted(set(observation.uncertainty_notes + analysis.uncertainties))
    observation.__post_init__()
    return AnalyzedObservation(observation, analysis)


def load_analyzed_observation(acquisition_store, analysis_store, observation_id):
    pointer = analysis_store.read(f"analyses/{observation_id}/latest.json")
    if not pointer:
        return None
    analysis = ObservationAnalysis.from_dict(analysis_store.read(pointer["path"]))
    content = acquisition_store.load_content(observation_id)
    if not content or not any(m.checksum == analysis.media_sha256 for m in content.media):
        raise ValueError("Analysis does not match acquired media identity.")
    return project_observation(content, analysis, pointer["source_id"])


class ObservationAnalysisService:
    def __init__(self, acquisition_store, analysis_store, analyzer, *, inspector=inspect_media, frame_extractor=extract_frame):
        self.acquisition_store, self.store, self.analyzer = acquisition_store, analysis_store, analyzer
        self.inspector, self.frame_extractor = inspector, frame_extractor
        self.last_attempt = None

    def analyze(self, content, *, force=False):
        self.last_attempt = None
        media = next((m for m in content.media if m.state.value == "local_ready" and m.local_path), None)
        if not media:
            raise ValueError("Analysis requires acquired local video.")
        path = self.acquisition_store.path(media.local_path)
        config = asdict(self.analyzer.config)
        prompt_version = getattr(self.analyzer, "prompt_version", PROMPT_VERSION)
        fingerprint = hashlib.sha256(json.dumps({"checksum": media.checksum, "config": config,
            "schema": SCHEMA_VERSION, "prompt": prompt_version, "speech": self.analyzer.speech_transcriber.identity() if getattr(self.analyzer, "speech_transcriber", None) else None, "processing_version": 3}, sort_keys=True).encode()).hexdigest()
        pointer_path = f"analyses/{content.observation.id}/latest.json"
        pointer = self.store.read(pointer_path)
        inspection = self.inspector(path, media.checksum)
        if not force and pointer and pointer.get("fingerprint") == fingerprint:
            return load_analyzed_observation(self.acquisition_store, self.store, content.observation.id), True
        analysis_id = new_id()
        folder = f"analyses/{content.observation.id}/{analysis_id}"
        started = time.monotonic()
        attempt = {"id": analysis_id, "observation_id": content.observation.id, "media_sha256": media.checksum,
                   "config": config, "started_at": now(), "status": "running", "fingerprint": fingerprint}
        write_safe(self.store, folder + "/attempt.json", attempt)
        raw_source = Source(content.brand_id, SourceType.FILE, "Original acquired reference video", str(path),
            metadata={"role": "analysis_original_media", "observation_id": content.observation.id, "sha256": media.checksum,
                      "origin_source_references": media.source_references})
        self.store.save_source(raw_source)
        try:
            payload = self.analyzer.analyze(path, inspection)
            response_source = Source(content.brand_id, SourceType.FILE, "Video analysis model response", folder + "/model-response.json",
                metadata={"role": "analysis_model_response", "model": config["model"], "prompt_version": prompt_version,
                          "observation_id": content.observation.id, "media_sha256": media.checksum})
            write_safe(self.store, response_source.url_or_path, self.analyzer.last_response, immutable=True)
            self.store.save_source(response_source)
            evidence = [MediaEvidence(**{**e, "source_references": [raw_source.id, response_source.id]}) for e in payload["evidence"]]
            features = {k: CreativeFeature(**v) for k, v in payload["features"].items()}
            coverage = dict(payload["coverage"])
            uncertainties = list(payload["uncertainties"])
            unsupported_features = []
            input_sources = []
            manifest_path = self.analyzer.last_diagnostics.get("input_manifest")
            if manifest_path:
                manifest = self.store.read(manifest_path)
                manifest_source = Source(content.brand_id, SourceType.FILE, "Multimodal model input manifest", manifest_path,
                    metadata={"role": "analysis_input_manifest", "media_sha256": media.checksum,
                              "prompt_version": prompt_version})
                self.store.save_source(manifest_source)
                input_sources.append(manifest_source.id)
                frame_sources = []
                for frame in manifest["frames"]:
                    source = Source(content.brand_id, SourceType.FILE, "Frame supplied to multimodal analysis", frame["path"],
                        metadata={"role": "analysis_input_frame", "media_sha256": media.checksum,
                            "sha256": frame["sha256"], "requested_time_seconds": frame["timestamp_seconds"],
                            "method": "ffmpeg", "timestamp_precision": "requested source seek"})
                    self.store.save_source(source)
                    frame_sources.append((frame["timestamp_seconds"], source.id))
                audio_source = None
                if manifest["audio"]:
                    audio = manifest["audio"]
                    audio_source = Source(content.brand_id, SourceType.FILE, "Audio supplied to multimodal analysis", audio["path"],
                        metadata={"role": "analysis_input_audio", "media_sha256": media.checksum,
                            "sha256": audio["sha256"], "start_seconds": 0, "method": "ffmpeg"})
                    self.store.save_source(audio_source)
                    input_sources.append(audio_source.id)
                for item in evidence:
                    item.source_references.append(manifest_source.id)
                    if item.kind in {"speech", "audio"} and audio_source:
                        item.source_references.append(audio_source.id)
                        item.method = "model_extraction_from_audio"
                    elif item.kind in {"visual", "on_screen_text", "cut"}:
                        linked = [ref for ts, ref in frame_sources if item.start_seconds <= ts <= item.end_seconds]
                        if not linked:
                            closest = min(frame_sources, key=lambda pair: abs(pair[0] - item.start_seconds))
                            linked = [closest[1]]
                            item.uncertainty_note = (item.uncertainty_note or "") + (
                                " Nearest input frame is a review anchor; it does not verify this evidence span.")
                        item.source_references.extend(linked)
                        item.method = "model_extraction_from_sampled_frames"
                uncertainties.extend(manifest.get("limitations", []))
            transcript_path = self.analyzer.last_diagnostics.get("transcript_path")
            if transcript_path:
                transcript = self.store.read(transcript_path)
                transcript_source = Source(content.brand_id, SourceType.FILE, "Local timestamped speech transcript", transcript_path,
                    metadata={"role": "analysis_transcript", "media_sha256": media.checksum,
                              "language": transcript["language"], "provenance": transcript["provenance"]})
                self.store.save_source(transcript_source)
                input_sources.append(transcript_source.id)
                for item in evidence:
                    if item.kind == "speech":
                        item.source_references = [raw_source.id, transcript_source.id]
                        item.method = "local_whisper_transcription"
            by_id = {e.id: e for e in evidence}
            for dimension, feature in features.items():
                wrong_audio = dimension == "audio" and inspection["has_audio"] and feature.category != "unknown" and not any(
                    by_id.get(ref) and by_id[ref].kind in {"audio", "speech"} for ref in feature.evidence_refs)
                if feature.category in VOCABULARY.get(dimension, []) and feature.category != "unknown" and (
                        not feature.evidence_refs or wrong_audio):
                    # Retain the raw response, but do not promote an unsupported interpretation.
                    features[dimension] = CreativeFeature("unknown", None, [])
                    unsupported_features.append(dimension)
                    uncertainties.append(f"{dimension}: model supplied no modality-appropriate supporting evidence; interpretation withheld.")
                    if wrong_audio:
                        coverage["audio"] = "uncertain"
            if not inspection["has_audio"]:
                # Never erase contradictory model claims to manufacture success; validation rejects them.
                evidence.append(MediaEvidence("media-properties", "media_properties", 0, inspection["duration_seconds"],
                    "FFprobe found no audio stream in this file.", source_references=[raw_source.id], method="ffprobe"))
                if features["audio"].category == "unknown":
                    features["audio"] = CreativeFeature("silent", "No audio stream in the acquired file.", ["media-properties"])
            if content.observation.duration_seconds is not None and abs(content.observation.duration_seconds - inspection["duration_seconds"]) > 0.25:
                uncertainties.append("Provider duration differs from the local container duration; original acquisition value retained.")
            status = "partial" if unsupported_features or any(v in {"uncertain", "unavailable"} for k, v in coverage.items()
                if k in {"video", "on_screen_text"} or inspection["has_audio"]) else "completed"
            for cut in payload["cut_candidates"]:
                cut["method"] = "model_approximate"
            analysis = ObservationAnalysis(analysis_id, content.observation.id, media.checksum, config["model"], now(), inspection,
                evidence, features, coverage, payload["subject_labels"], payload["timeline"], payload["cut_candidates"],
                sorted(set(uncertainties)), [raw_source.id, response_source.id, *input_sources], status=status, usage=model_usage(self.analyzer),
                prompt_version=prompt_version)
            frame_count = 0
            for e in analysis.evidence:
                if e.kind not in {"visual", "on_screen_text", "cut"} or frame_count >= config["max_evidence_frames"]:
                    continue
                requested_time = min((e.start_seconds + e.end_seconds) / 2, inspection["duration_seconds"] - 0.05)
                frame_path = f"{folder}/frames/{frame_count:02d}.jpg"
                try:
                    self.frame_extractor(path, self.store.path(frame_path), requested_time)
                    source = Source(content.brand_id, SourceType.FILE, "Timestamped local evidence frame", frame_path,
                        metadata={"role": "analysis_frame", "media_sha256": media.checksum, "requested_time_seconds": requested_time,
                                  "evidence_id": e.id, "method": "ffmpeg", "timestamp_precision": "requested seek, not exact cut timing"})
                    self.store.save_source(source)
                    e.source_references.append(source.id)
                    frame_count += 1
                except (OSError, ValueError, subprocess.SubprocessError):
                    analysis.uncertainties.append(f"Local frame unavailable for {e.id}; original video remains the evidence.")
                    analysis.status = "partial"
            analysis.timings = {"total_seconds": time.monotonic() - started, **self.analyzer.last_diagnostics}
            analysis.__post_init__()
            analysis_source = Source(content.brand_id, SourceType.FILE, "Structured individual video observation", folder + "/analysis.json",
                metadata={"role": "observation_analysis", "observation_id": content.observation.id, "media_sha256": media.checksum,
                          "schema_version": SCHEMA_VERSION, "analyzed_at": analysis.analyzed_at})
            write_safe(self.store, analysis_source.url_or_path, analysis.to_dict(), immutable=True)
            self.store.save_source(analysis_source)
            enriched = project_observation(content, analysis, analysis_source.id)
            write_safe(self.store, folder + "/observation.json", enriched.observation.to_dict(), immutable=True)
            self.store.path(folder + "/validation.md").write_text(redact_text(validation_report(enriched, path, self.store)), encoding="utf-8")
            write_safe(self.store, pointer_path, {"path": analysis_source.url_or_path, "source_id": analysis_source.id,
                                                "fingerprint": fingerprint, "report_path": folder + "/validation.md"})
            attempt["status"] = analysis.status
            return enriched, False
        except (ValueError, KeyError, TypeError, IndexError, AcquisitionFailure, OSError, subprocess.SubprocessError) as e:
            attempt["status"] = "failed"
            attempt["error"] = e.error.to_dict() if isinstance(e, AcquisitionFailure) else {
                "code": "invalid_analysis" if isinstance(e, (ValueError, KeyError, TypeError, IndexError)) else "local_media_failure",
                "message": str(e) if isinstance(e, ValueError) else "Analysis unavailable; inspect retained response and diagnostics."}
            write_safe(self.store, folder + "/model-response.json", self.analyzer.last_response)
            raise
        finally:
            attempt["ended_at"], attempt["elapsed_seconds"] = now(), time.monotonic() - started
            attempt["diagnostics"] = self.analyzer.last_diagnostics
            attempt["usage"] = model_usage(self.analyzer)
            write_safe(self.store, folder + "/attempt.json", attempt)
            self.last_attempt = {**attempt, "report_path": str(self.store.path(folder + "/validation.md"))}
            if attempt["status"] == "failed":
                # An inspectable failure report is not a ContentObservation or creative analysis.
                lines = [f"# {content.observation.content_url}", "", f"[Play original Reel]({path})", "",
                         "**Analysis failed; human review and real-world acceptance remain pending.**", "",
                         f"Inspected duration: {inspection['duration_seconds']:.3f}s; audio stream present: {inspection['has_audio']}.",
                         "Technical stream presence does not prove audio or video understanding.", ""]
                lines += [f"- {dimension}: unknown (no validated model output)" for dimension in VOCABULARY]
                lines += ["- Multimodal interpretation, on-screen text, scenes and temporal creative features: unavailable.",
                          "", f"Failure: `{json.dumps(attempt['error'])}`", "",
                          f"Latency: {attempt['elapsed_seconds']:.2f}s; usage: `{json.dumps(attempt['usage'])}`", "",
                          f"Media SHA-256: `{inspection['sha256']}`; original provenance: {', '.join(media.source_references)}."]
                transcript_path = attempt["diagnostics"].get("transcript_path")
                if transcript_path:
                    transcript = self.store.read(transcript_path)
                    lines += ["", f"[Retained local speech evidence]({self.store.path(transcript_path)})",
                              f"Speech: {transcript['speech_status']}; language: {transcript['language']}; zero API cost."]
                    lines += [f"- {s['id']} {s['start_seconds']:.2f}–{s['end_seconds']:.2f}s: {s['text']}" for s in transcript['segments']]
                self.store.path(folder + "/validation.md").write_text(redact_text("\n".join(lines) + "\n"), encoding="utf-8")


def validation_report(enriched, media_path, store):
    analysis = enriched.analysis
    lines = [f"# {enriched.observation.content_url}", "", f"[Play original Reel]({media_path})", "",
             f"Status: {analysis.status}; **human review pending**. Model annotations are not verified factual assertions.", "",
             f"Media duration: {analysis.media_inspection['duration_seconds']:.3f}s; coverage: {json.dumps(analysis.coverage)}", ""]
    for dimension, feature in analysis.features.items():
        lines += [f"- **{dimension}**: `{feature.category}` — {feature.description or 'No supported detail'}; evidence: {', '.join(feature.evidence_refs) or 'unknown'}"]
    lines += ["", "Subject labels: " + ", ".join(analysis.subject_labels), "", "## Extracted evidence (machine-produced)", ""]
    for e in sorted(analysis.evidence, key=lambda e: e.start_seconds):
        links = []
        for ref in e.source_references:
            source = store.read(f"sources/{ref}.json")
            if source and source["metadata"].get("role") in {"analysis_frame", "analysis_input_frame", "analysis_input_audio"}:
                role = source["metadata"]["role"]
                label = "audio fourni" if role == "analysis_input_audio" else "image fournie" if role == "analysis_input_frame" else "image de contrôle"
                links.append(f"[{label}]({store.path(source['url_or_path'])})")
        lines.append(f"- `{e.id}` {e.start_seconds:.2f}–{e.end_seconds:.2f}s **{e.kind}**: {e.text} "
                     + " ".join(links) + (f" Uncertainty: {e.uncertainty_note}" if e.uncertainty_note else ""))
    lines += ["", "## Sequence and approximate cuts", ""]
    for segment in analysis.timeline:
        lines.append(f"- {segment['start_seconds']:.2f}–{segment['end_seconds']:.2f}s {segment['role']}: {', '.join(segment['evidence_refs'])}")
    lines += ["", "Approximate cut candidates: " + (", ".join(f"{c['time_seconds']:.2f}s" for c in analysis.cut_candidates) or "none recorded; not proof of zero cuts"),
              "", "## Uncertainty and usage", "", *["- " + u for u in analysis.uncertainties], "",
              f"Latency: {analysis.timings['total_seconds']:.2f}s; usage: `{json.dumps(analysis.usage)}`", "",
              "Sampling may miss brief text, rapid cuts or fine details. Validate against playback, not captions alone."]
    if analysis.timings.get("response_reused"):
        original_elapsed = analysis.timings.get("original_elapsed_seconds")
        latency = f"{original_elapsed:.2f}s" if original_elapsed is not None else "unknown (consult original comparison report)"
        lines += ["", f"Retained real-provider response reprocessed locally with zero new provider requests. "
                  f"Original live latency: {latency}. "
                  "Reported tokens/cost belong to that original call, not this local reprocessing."]
    return "\n".join(lines) + "\n"
