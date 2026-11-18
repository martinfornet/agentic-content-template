#!/bin/sh
# Integration test with a live model, real synthesis and generated footage.
set -eu
cd "$(dirname "$0")/.."
mkdir -p output
run_dir=$(mktemp -d "$PWD/output/smoke.XXXXXX")
for color in orange blue; do
  ffmpeg -v error -f lavfi -i "color=c=$color:s=360x640:r=30" -t 5 -c:v libx264 "$run_dir/$color.mp4"
done
.venv/bin/python -m creator --root "$run_dir/projects" start "Imagine une très courte vidéo avec deux aplats de couleur, orange puis bleu." <<ANSWERS
test-technique
$run_dir/orange.mp4
$run_dir/blue.mp4

oui
A
oui
non
ANSWERS
.venv/bin/python - "$run_dir" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

project_file, = (Path(sys.argv[1]) / "projects").glob("*.json")
project = json.loads(project_file.read_text())
assert project["stage"] == "assembled"
timeline = project["assembly"]["timeline"]
assert " ".join(c["text"] for c in timeline["captions"]) == project["proposal"]["script"]
assert len(timeline["shots"]) == 2
assert timeline["music"]["bpm"] == 90
assert timeline["music"]["voice_sha256"] == project["voice"]["sha256"]
output = project["assembly"]["path"]
streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                             "-of", "json", output]))["streams"]
video, audio = streams
assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1080, 1920)
assert audio["codec_name"] == "aac" and float(audio["duration"]) > 0
assert abs(float(video["duration"]) - float(audio["duration"])) < 0.1
subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", output, "-f", "null", "-"], check=True)
print(output)
PY
