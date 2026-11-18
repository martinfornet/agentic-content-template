"""Transparent subtitle overlays, separate from the cached voice worker."""
import argparse
import json
from pathlib import Path


if __name__ == "__main__" and not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from creator.timing import captions


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
