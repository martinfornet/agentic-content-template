"""Deterministic media properties and timestamped evidence frames; no interpretation."""
import hashlib
import json
import math
import subprocess
from fractions import Fraction


def inspect_media(path, expected_checksum):
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("Local video is missing or empty.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != expected_checksum:
        raise ValueError("Media checksum differs from acquisition evidence.")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                           capture_output=True, check=True, timeout=30)
    data = json.loads(probe.stdout)
    videos = [s for s in data["streams"] if s.get("codec_type") == "video"]
    if not videos:
        raise ValueError("Media has no video stream.")
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cannot establish a finite media duration.")
    try:
        rate = float(Fraction(videos[0].get("avg_frame_rate", "0/1")))
        rate = rate if math.isfinite(rate) and rate > 0 else None
    except (ValueError, ZeroDivisionError):
        rate = None
    video_duration = float(videos[0].get("duration", duration))
    if not math.isfinite(video_duration) or video_duration <= 0:
        raise ValueError("Cannot establish a finite video stream duration.")
    return {"duration_seconds": duration, "has_audio": any(s.get("codec_type") == "audio" for s in data["streams"]),
            "video_duration_seconds": video_duration, "frame_rate": rate,
            "width": videos[0]["width"], "height": videos[0]["height"], "video_codec": videos[0]["codec_name"],
            "container": data["format"]["format_name"], "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def extract_frame(video, path, time_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-ss", str(time_seconds), "-i", str(video), "-frames:v", "1",
                    "-q:v", "2", "-y", str(path)], capture_output=True, check=True, timeout=30)
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("No frame available at the requested timestamp.")


def prepare_decomposed_media(video, inspection, root, relative, config):
    """Persist bounded real inputs before generation; times are requested source seeks."""
    import re
    from app.services.analysis_io import write_safe
    from app.services.acquisition_store import AcquisitionStore
    store = AcquisitionStore(root)
    duration = inspection['duration_seconds']
    video_duration = min(duration, inspection.get('video_duration_seconds', duration))
    frame_interval = 1 / inspection['frame_rate'] if inspection.get('frame_rate') else .3
    end = max(0, video_duration - max(.05, frame_interval) - .001)
    opening = [min(t, end) for t in (0, .5, 1, 2, 3)[:min(5, config.representative_frames - 1)]]
    uniform = [round(i * end / (config.representative_frames - 1), 6)
               for i in range(config.representative_frames)]
    # Opening + uniform samples fit the representative budget; reserve scene slots separately.
    times = sorted(set(opening + uniform))
    if len(times) > config.representative_frames:
        interior = [t for t in times if t not in opening and t != end]
        slots = config.representative_frames - len(set(opening + [end]))
        times = sorted(set(opening + [end] + [interior[round(i * (len(interior) - 1) / max(1, slots - 1))]
                                               for i in range(max(0, slots))]))
    scenes = []
    if config.scene_frames:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(video), '-vf',
            f"select='gt(scene,{config.scene_threshold})',showinfo", '-an', '-f', 'null', '-'],
            capture_output=True, text=True, check=True, timeout=60)
        scenes = sorted(set(float(t) for t in re.findall(r'pts_time:([0-9.]+)', result.stderr)
                            if 0 <= float(t) <= end))
        # Spread the bounded extra samples through the full clip, not just its beginning.
        count = min(config.scene_frames, len(scenes))
        selected = [scenes[round(i * (len(scenes) - 1) / max(1, count - 1))] for i in range(count)]
        times = sorted(set(times + selected))
    manifest = {'source_media_sha256': inspection['sha256'], 'duration_seconds': duration,
        'method': 'ffmpeg_timestamped_frames_and_audio', 'extraction_version': 1,
        'scene_threshold': config.scene_threshold, 'scene_candidates': scenes, 'frames': [], 'audio': None,
        'limitations': ['Sparse frames do not establish continuous motion or exact editing rhythm.',
                        'Frame timestamps are requested seeks; scene scores are candidates, not verified cuts.']}
    total = 0
    for index, ts in enumerate(times):
        relative_path = f'{relative}/frame-{index:02d}.jpg'
        target = store.path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(ts), '-i', str(video), '-frames:v', '1',
            '-vf', f'scale={config.frame_width}:-2', '-q:v', '3', '-y', str(target)],
            capture_output=True, check=True, timeout=30)
        if not target.is_file() or not target.stat().st_size:
            raise ValueError('Representative frame is unavailable.')
        total += target.stat().st_size
        manifest['frames'].append({'path': relative_path, 'timestamp_seconds': ts,
                                  'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    if inspection['has_audio']:
        existing = inspection.get('extracted_audio')
        relative_path = existing['path'] if existing else f'{relative}/audio.wav'
        target = store.path(relative_path)
        if existing:
            if hashlib.sha256(target.read_bytes()).hexdigest() != existing['sha256']:
                raise ValueError('Transcript audio checksum mismatch.')
        else:
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-vn', '-ac', '1', '-ar', '16000',
                '-c:a', 'pcm_s16le', '-y', str(target)], capture_output=True, check=True, timeout=30)
        if not target.is_file() or not target.stat().st_size:
            raise ValueError('Audio extraction is unavailable.')
        total += target.stat().st_size
        manifest['audio'] = {'path': relative_path, 'start_seconds': 0, 'mime_type': 'audio/wav',
                            'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    if total * 4 // 3 > config.max_input_bytes:
        raise ValueError('Extracted media exceeds configured inline request limit.')
    write_safe(store, f'{relative}/manifest.json', manifest)
    return manifest


def input_parts(root, manifest, max_bytes):
    """Load exactly the retained inputs; verify checksums and bounded payload size."""
    import base64
    from pathlib import Path
    root = Path(root).resolve()
    if not manifest['frames'] or len(manifest['frames']) > 96 or not math.isfinite(manifest['duration_seconds']):
        raise ValueError('Invalid retained input manifest.')
    parts = []
    total = 0
    for item in [*manifest['frames'], *([manifest['audio']] if manifest['audio'] else [])]:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Input artifact escapes analysis storage.')
        if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
            raise ValueError('Input artifact is missing, empty or too large.')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Input artifact checksum mismatch.')
        total += len(data)
        if total * 4 // 3 > max_bytes:
            raise ValueError('Inline media exceeds configured request limit.')
        if 'timestamp_seconds' in item:
            if not 0 <= item['timestamp_seconds'] <= manifest['duration_seconds']:
                raise ValueError('Input frame timestamp exceeds source duration.')
            parts.append({'text': f"Source frame timestamp {item['timestamp_seconds']:.6f} seconds (requested seek)."})
        parts.append({'inlineData': {'mimeType': item.get('mime_type', 'image/jpeg'),
                                    'data': base64.b64encode(data).decode()}})
    return parts
