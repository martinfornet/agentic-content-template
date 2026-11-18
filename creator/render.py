import json
import math
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


def render(clips, voice, output):
    seconds = float(probe(voice)["format"]["duration"])
    frames = math.ceil(seconds * 30)
    if frames < len(clips):
        raise ValueError("La narration est trop courte pour le nombre de plans.")
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        root = Path(temp)
        files = []
        for i, clip in enumerate(clips):
            count = (i + 1) * frames // len(clips) - i * frames // len(clips)
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
        encode(["-f", "concat", "-safe", "1", "-i", str(manifest), "-i", str(voice),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                "-b:a", "160k", "-movflags", "+faststart", str(candidate)])
        candidate.replace(output)
