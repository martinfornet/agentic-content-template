"""Subprocess worker in the existing local Chatterbox environment; offline only."""
import argparse
import json
import os
from pathlib import Path
import random
import hashlib


def captions(text, name, output, font, preview=False):
    from PIL import Image, ImageDraw, ImageFont
    canvas = Image.new("RGBA", (1080, 1920))
    draw = ImageDraw.Draw(canvas)
    face = ImageFont.truetype(font, 58)
    words, lines, line = text.split(), [], ""
    for word in words:
        proposal = (line + " " + word).strip()
        if draw.textlength(proposal, font=face) > 850 and line:
            lines.append(line)
            line = word
        else:
            line = proposal
    if line:
        lines.append(line)
    if len(lines) > 5:
        raise ValueError("Sous-titre trop long : raccourcir ou découper ce segment.")
    y = 1380 - len(lines) * 38
    draw.rounded_rectangle((76, y - 24, 1004, y + 76 * len(lines) + 14), radius=22, fill=(0, 0, 0, 205))
    for index, line in enumerate(lines):
        x = (1080 - draw.textlength(line, font=face)) / 2
        draw.text((x, y + index * 76), line, font=face, fill="white", stroke_width=1, stroke_fill="black")
    label = ImageFont.truetype(font, 42)
    draw.rounded_rectangle((76, 200, 1004, 290), radius=20, fill=(0, 0, 0, 160))
    draw.text((100, 217), name[:40], font=label, fill="white")
    if preview:
        small = ImageFont.truetype(font, 29)
        draw.rounded_rectangle((76, 305, 1004, 370), radius=12, fill=(80, 47, 0, 225))
        draw.text((100, 319), "VERSION DE TRAVAIL · FAITS NON VALIDÉS", font=small, fill="white")
    canvas.save(output)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["voice", "caption", "inspect"])
    p.add_argument("manifest")
    args = p.parse_args()
    values = json.loads(Path(args.manifest).read_text())
    if args.mode == "caption":
        for item in values["items"]:
            captions(item["text"], values["name"], item["output"], values["font"], values.get("preview", False))
        return
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    import numpy as np
    import torch
    import torchaudio
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    torch.set_num_threads(4)
    model = ChatterboxMultilingualTTS.from_local(Path(values["checkpoint"]), device="cpu", t3_model="v3")
    model.prepare_conditionals(values["reference"], exaggeration=0.5)
    if args.mode == "inspect":
        print(json.dumps({"model_loaded": True, "reference_loaded": True, "device": "cpu", "model": "v3", "generated_audio": False}))
        return
    for index, item in enumerate(values["items"]):
        seed = item["seed"]
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        wav = model.generate(item["text"], language_id="fr", exaggeration=0.5, cfg_weight=0.5,
                             temperature=0.8, repetition_penalty=1.2, min_p=0.05, top_p=1.0)
        target = Path(item["output"])
        temporary = target.with_suffix(".tmp.wav")
        torchaudio.save(str(temporary), wav.cpu(), model.sr, encoding="PCM_S", bits_per_sample=16)
        temporary.replace(target)
        receipt = target.with_suffix(".json")
        temp_receipt = receipt.with_suffix(".tmp")
        temp_receipt.write_text(json.dumps({"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                           "identity": values["identity"], "text": item["text"], "seed": seed}))
        temp_receipt.replace(receipt)


if __name__ == "__main__":
    main()
