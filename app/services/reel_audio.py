"""Delivered audio measurements and optional voice-led music audition mixes.

Targets below are internal production targets, not platform recommendations or
a judgment of voice naturalness. Music rights and editorial approval are separate.
"""
import json
import math
from pathlib import Path
import re
import subprocess
import wave
import tempfile


def shorten_long_pauses(voice, output, *, minimum_pause=1.5, retained_pause=.6,
                        silence_threshold_db=-40, ffmpeg="ffmpeg"):
    """Trim only the middle of long quiet gaps in a dry PCM voice, before ASR.

    This reproduces the C audition's pause edit, not an artistic approval.
    Speech samples and shorter pauses remain unchanged; no time stretching.
    """
    voice, output = Path(voice), Path(output)
    if voice.resolve() == output.resolve():
        raise ValueError("Le montage des pauses ne peut pas remplacer la source.")
    if (not all(math.isfinite(v) for v in (minimum_pause, retained_pause, silence_threshold_db))
            or not .8 <= minimum_pause <= 10 or not .2 <= retained_pause < minimum_pause
            or not -60 <= silence_threshold_db <= -35):
        raise ValueError("Réglages de pauses hors limites.")
    detection = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(voice), "-vn", "-af",
         f"silencedetect=noise={silence_threshold_db}dB:d={minimum_pause}", "-f", "null", "-"],
        capture_output=True, text=True, check=True, timeout=120)
    with wave.open(str(voice), "rb") as stream:
        params = stream.getparams()
        if params.sampwidth != 2 or params.comptype != "NONE":
            raise ValueError("Les pauses nécessitent une voix PCM 16 bits.")
        data = stream.readframes(params.nframes)
    rate, size = params.framerate, params.nchannels * params.sampwidth
    duration, start, silences = params.nframes / rate, None, []
    for match in re.finditer(r"silence_(start|end):\s*([0-9.e+-]+)", detection.stderr):
        if match[1] == "start":
            start = float(match[2])
        elif start is not None:
            silences.append((start, min(float(match[2]), duration)))
            start = None
    if start is not None:
        silences.append((start, duration))
    cursor, parts, cuts = 0, [], []
    for start, end in silences:
        if end - start <= minimum_pause:
            continue
        begin_frame = round((start + retained_pause / 2) * rate)
        end_frame = round((end - retained_pause / 2) * rate)
        if not cursor <= begin_frame < end_frame <= params.nframes:
            raise ValueError("Détection de silence incohérente.")
        parts.append(data[cursor * size:begin_frame * size])
        cuts.append({"start_frame": begin_frame, "end_frame": end_frame,
                     "removed_seconds": (end_frame - begin_frame) / rate})
        cursor = end_frame
    parts.append(data[cursor * size:])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".wav", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with wave.open(str(temporary), "wb") as stream:
            stream.setparams(params)
            stream.writeframes(b"".join(parts))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"policy": {"minimum_pause": minimum_pause, "retained_pause": retained_pause,
                        "silence_threshold_db": silence_threshold_db},
            "source_duration": duration, "duration": sum(map(len, parts)) / size / rate,
            "cuts": cuts, "speed_changed": False, "naturalness_assessed": False}


def measure_audio(path, *, ffmpeg="ffmpeg"):
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-vn",
         "-af", "silencedetect=noise=-40dB:d=0.35,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True, timeout=120,
    )
    blocks = re.findall(r'\{\s*"input_i".*?\}', result.stderr, re.S)
    if not blocks:
        raise ValueError("Mesure audio indisponible : aucun résultat loudnorm.")
    raw = json.loads(blocks[-1])

    def finite(key):
        value = float(raw[key])
        return value if math.isfinite(value) else None

    loudness, peak = finite("input_i"), finite("input_tp")
    checks = {
        "audible": loudness is not None and peak is not None,
        "loudness": loudness is not None and -18 <= loudness <= -14,
        "true_peak": peak is not None and peak <= -0.8,
    }
    silences = []
    start = None
    for match in re.finditer(r"silence_(start|end):\s*([0-9.e+-]+)", result.stderr):
        kind, value = match[1], float(match[2])
        if kind == "start":
            start = value
        elif start is not None:
            silences.append({"start": start, "end": value, "duration": value - start})
            start = None
    if start is not None:
        silences.append({"start": start, "end": None, "duration": None})
    return {
        "passed": all(checks.values()), "checks": checks,
        "integrated_lufs": loudness, "true_peak_dbtp": peak,
        "loudness_range_lu": finite("input_lra"), "silences": silences,
        "policy": {"loudness_lufs": [-18, -14], "maximum_true_peak_dbtp": -0.8},
        "naturalness_assessed": False,
        "note": "Mesures techniques uniquement ; respirations et expressivité à écouter.",
    }


def mix_music(voice, music, output, *, duration, gain_db=-12,
              ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """Mix parallel voice and music without speech-dependent music gain."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Durée invalide.")
    if not math.isfinite(gain_db) or not -24 <= gain_db <= -6:
        raise ValueError("Gain musical hors limites (-24 à -6 dB).")
    output = Path(output)
    if output.resolve() in {Path(voice).resolve(), Path(music).resolve()}:
        raise ValueError("Le mix ne peut pas remplacer une source.")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(music)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    info = json.loads(probe.stdout)
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise ValueError("Piste musicale sans audio.")
    if float(info["format"]["duration"]) + .05 < duration:
        raise ValueError("Piste musicale trop courte ; aucune boucle implicite.")
    fade = min(.6, duration / 3)
    graph = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,"
        "loudnorm=I=-16:TP=-2:LRA=11[v];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={duration},"
        f"asetpts=PTS-STARTPTS,loudnorm=I=-18:TP=-2:LRA=11,volume={gain_db}dB,"
        f"afade=t=in:d={fade},afade=t=out:st={duration-fade}:d={fade}[bed];"
        "[v][bed]amix=inputs=2:duration=first:normalize=0[out]"
    )
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(voice), "-i", str(music),
         "-filter_complex", graph, "-map", "[out]", "-t", str(duration),
         "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return measure_audio(output, ffmpeg=ffmpeg)
