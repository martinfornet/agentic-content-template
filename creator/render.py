import json
import math
import os
import hashlib
from .timing import align, subtitle_chunks, captions, snap_music_cuts
from pathlib import Path
import subprocess
import tempfile


def probe(path):
    result = subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                                      "-of", "json", str(path)])
    return json.loads(result)


def encode(args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def whisper_model():
    override = os.getenv("CREATOR_WHISPER_MODEL")
    if override:
        path = Path(override).expanduser()
    else:
        from huggingface_hub import hf_hub_download
        path = Path(hf_hub_download("ggerganov/whisper.cpp", "ggml-small.bin"))
    with path.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b":
            raise ValueError("Utilisez le modèle multilingue ggml-small.bin indiqué dans le README.")
    return path


def track_captions(root, voice, script, seconds):
    audio = root / "asr.wav"
    encode(["-i", str(voice), "-ar", "16000", "-ac", "1", str(audio)])
    prefix = root / "words"
    subprocess.run(["whisper-cli", "-ng", "-m", str(whisper_model()), "-f", str(audio),
                    "-l", "fr", "-ojf", "-ml", "1", "-sow", "-of", str(prefix),
                    "-tp", "0", "-nf", "-mc", "0"], stdout=subprocess.DEVNULL, check=True)
    chunks = subtitle_chunks([{"text": script}])
    aligned = align(chunks, json.loads(prefix.with_suffix(".json").read_text()), seconds, .1)
    times = aligned["boundaries"]
    return [dict(text=c["text"], start=a, end=b) for c, a, b in zip(chunks, times, times[1:])]


def plan_shots(count, frames, items, pulses):
    if frames < 9 * count:
        raise ValueError("Trop de prises pour conserver au moins 0,3 seconde par plan.")
    boundaries = [0]
    breaks = [round(item["start"] * 30) for item in items[1:]]
    for i in range(1, count):
        target = round(i * frames / count)
        lo = boundaries[-1] + 9
        hi = frames - 9 * (count - i)
        near = [b for b in breaks if lo <= b <= hi and abs(b - target) <= 15]
        boundaries.append(min(near, key=lambda b: abs(b-target)) if near else max(lo, min(target, hi)))
    boundaries.append(frames)
    shots = [dict(start=a/30, end=b/30) for a, b in zip(boundaries, boundaries[1:])]
    return snap_music_cuts(shots, pulses)


def render(clips, voice, output, script, music=None):
    seconds = float(probe(voice)["format"]["duration"])
    frames = math.ceil(seconds * 30)
    if frames < len(clips):
        raise ValueError("La narration est trop courte pour le nombre de plans.")
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        root = Path(temp)
        items = track_captions(root, voice, script, seconds)
        shots = plan_shots(len(clips), frames, items, music["pulses"] if music else [])
        files = []
        for i, clip in enumerate(clips):
            count = round(shots[i]["end"] * 30) - round(shots[i]["start"] * 30)
            part = root / f"part-{i}.mp4"
            filters = ("fps=30,scale=1080:1920:force_original_aspect_ratio=decrease,"
                       "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                       f"tpad=stop_mode=clone:stop_duration={count / 30:.6f}")
            encode(["-i", str(clip), "-map", "0:v:0", "-an", "-vf", filters, "-frames:v",
                    str(count), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(part)])
            files.append(f"file '{part.name}'")
        manifest = root / "parts.txt"
        manifest.write_text("\n".join(files))
        candidate = root / "video.mp4"
        fonts = [os.getenv("CREATOR_FONT"), "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        font = next((f for f in fonts if f and Path(f).is_file()), None)
        if not font:
            raise ValueError("Indiquez une police TrueType avec CREATOR_FONT.")
        args = ["-f", "concat", "-safe", "1", "-i", str(manifest),
                "-i", str(music["path"] if music else voice)]
        overlays = []
        previous = "0:v"
        for i, item in enumerate(items):
            png = root / f"caption-{i}.png"
            captions(item["text"], "", png, font)
            args.extend(["-i", str(png)])
            overlays.append(f"[{previous}][{i+2}:v]overlay=0:0:enable='gte(t,{item['start']})*lt(t,{item['end']})'[v{i}]")
            previous = f"v{i}"
        encode([*args, "-filter_complex", ";".join(overlays), "-map", f"[{previous}]", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "160k", "-movflags", "+faststart", str(candidate)])
        candidate.replace(output)
        return {"captions": items, "shots": shots, "music": music}
