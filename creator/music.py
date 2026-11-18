"""Two original instrumental beds. The beat grid is known at synthesis time."""
from pathlib import Path
import tempfile
import wave

from .render import encode, probe
from .workflow import _hash_file

STYLES = {"A": (90, (220, 261.63, 329.63, 293.66)), "B": (120, (196, 246.94, 293.66, 369.99))}


def previews(voice, folder):
    import numpy as np

    seconds = float(probe(voice["path"])["format"]["duration"])
    rate = 24000
    t = np.arange(round(seconds * rate)) / rate
    result = {}
    for name, (tempo, notes) in STYLES.items():
        beat = 60 / tempo
        phase = t % beat
        pitch = np.take(notes, (t / beat).astype(int) % len(notes))
        pluck = np.sin(2 * np.pi * pitch * phase) * np.exp(-phase * 7)
        bass = np.sin(2 * np.pi * (notes[0] / 2) * t) * np.exp(-phase * 13)
        fade = np.minimum(1, np.minimum(t / .04, (seconds - t) / .25))
        bed = (.035 * pluck + .035 * bass) * fade * np.minimum(1, phase / .009)
        target = folder / f"{Path(voice['path']).stem}-mix-{name}.wav"
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            wav = Path(tmp) / "bed.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setparams((1, 2, rate, 0, "NONE", ""))
                stream.writeframes((bed * 32767).astype("<i2").tobytes())
            mixed = Path(tmp) / "mix.wav"
            encode(["-i", voice["path"], "-i", str(wav), "-filter_complex",
                    "amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97:level=0:latency=1",
                    "-c:a", "pcm_s16le", str(mixed)])
            mixed.replace(target)
        result[name] = {"path": str(target), "sha256": _hash_file(target),
                        "voice_sha256": voice["sha256"], "bpm": tempo,
                        "pulses": np.arange(0, seconds, beat).tolist()}
    return result
