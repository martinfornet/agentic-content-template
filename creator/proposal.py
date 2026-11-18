"""Generate a structured brief using the creator's authenticated Codex CLI."""
import json
from pathlib import Path
import subprocess
import tempfile

FIELDS = ("idea", "shooting_brief", "script")
SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in FIELDS},
          "required": list(FIELDS), "additionalProperties": False}


def generate(direction, trends):
    prompt = (
        "Imagine une idée originale de vidéo verticale réalisable chez soi. Réponds en français. "
        "Donne une idée concrète, deux plans à filmer dans l'ordre, puis une seule phrase "
        "de narration de 8 à 16 mots. Aucune recherche ni appel d'outil. "
        "Les tendances sont seulement une inspiration. Paramètres créatifs JSON : "
        + json.dumps({"direction": direction, "tendances": trends}, ensure_ascii=False)
    )
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        schema = root / "schema.json"
        schema.write_text(json.dumps(SCHEMA))
        output = root / "proposal.json"
        subprocess.run(["codex", "exec", "--ephemeral", "--ignore-user-config",
                        "--skip-git-repo-check", "--sandbox", "read-only",
                        "--output-schema", str(schema), "-o", str(output), "-"],
                       input=prompt, text=True, cwd=root, check=True, timeout=300,
                       stdout=subprocess.DEVNULL)
        value = json.loads(output.read_text())
        return {key: value[key] for key in FIELDS}
