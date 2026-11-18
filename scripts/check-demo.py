"""Inspect the actual pixels and PCM of the orange/blue integration fixture."""
import json
from pathlib import Path
import subprocess
import sys

import numpy as np


def samples(path):
    data = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-vn",
                                    "-ar", "24000", "-ac", "1", "-f", "f32le", "-"])
    return np.frombuffer(data, dtype="<f4")


def frame(path, seconds):
    data = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
                                    "-frames:v", "1", "-vf", "scale=270:480", "-pix_fmt", "rgb24",
                                    "-f", "rawvideo", "-"])
    return np.frombuffer(data, dtype=np.uint8).reshape(480, 270, 3)


def check(project):
    output = project["assembly"]["path"]
    timeline = project["assembly"]["timeline"]
    for caption in timeline["captions"]:
        image = frame(output, (caption["start"] + caption["end"]) / 2)
        white = (image[270:400].min(axis=2) > 200).sum()
        assert white > 15, "Sous-titre absent des pixels du MP4"
    cut = timeline["shots"][1]["start"]
    red, _, blue = frame(output, cut - 1/30)[10, 10]
    assert red > blue, "Le plan orange doit précéder la coupe"
    red, _, blue = frame(output, cut + 1/30)[10, 10]
    assert blue > red, "Le plan bleu doit suivre la coupe"
    actual = samples(output)
    mix = samples(timeline["music"]["path"])
    dry = samples(project["voice"]["path"])
    n = min(map(len, (actual, mix, dry)))
    mix_error = np.linalg.norm(actual[:n] - mix[:n])
    dry_error = np.linalg.norm(actual[:n] - dry[:n])
    assert mix_error < 0.5 * dry_error, "Le MP4 doit contenir le mix sélectionné"
    print(f"{len(timeline['captions'])} groupes visibles, coupe à {cut:.2f} s, mix confirmé.")


if __name__ == "__main__":
    check(json.loads(Path(sys.argv[1]).read_text()))
