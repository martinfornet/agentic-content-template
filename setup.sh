#!/bin/sh
set -eu
cd "$(dirname "$0")"
for dep in ffmpeg ffprobe whisper-cli; do
  command -v "$dep" >/dev/null || { echo "Installer $dep (voir README.md)" >&2; exit 1; }
done
command -v claude >/dev/null || command -v codex >/dev/null || { echo "Installe Claude Code ou Codex CLI." >&2; exit 1; }
"${PYTHON:-python3.11}" -m venv .venv
.venv/bin/python -m pip install -r requirements-creator.txt
