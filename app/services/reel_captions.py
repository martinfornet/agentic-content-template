"""Transparent subtitle overlays, separate from the cached voice worker."""
import argparse
import json
from pathlib import Path


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
    for index, line in enumerate(lines):
        x = (1080 - draw.textlength(line, font=face)) / 2
        draw.text((x, y + index * 76), line, font=face, fill="white", stroke_width=2, stroke_fill="black")
    # Review status belongs in the studio, never in exported pixels.
    canvas.save(output)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["caption"])
    parser.add_argument("manifest")
    args = parser.parse_args()
    values = json.loads(Path(args.manifest).read_text())
    for item in values["items"]:
        captions(item["text"], values["name"], item["output"], values["font"], values.get("preview", False))


if __name__ == "__main__":
    main()
