#!/bin/sh
set -eu
cd "$(dirname "$0")"
for dep in ffmpeg ffprobe codex whisper-cli; do
  command -v "$dep" >/dev/null || { echo "Installer $dep (voir README.md)" >&2; exit 1; }
done
"${PYTHON:-python3.11}" -m venv .venv
.venv/bin/python -m pip install -r requirements-creator.txt
