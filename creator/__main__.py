"""Local CLI. Human decisions are explicit, and each step can resume later."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from .music import previews
from .proposal import generate
from .render import probe, render
from .speech import synthesize
from .workflow import CreatorWorkflow, _hash_file


def verify(record):
    if _hash_file(record["path"]) != record["sha256"]:
        raise ValueError(f"Fichier modifié depuis la validation : {record['path']}")


def run(args):
    flow = CreatorWorkflow(args.root)
    if args.action == "idea":
        proposal = generate(args.direction, args.trend, context=getattr(args, "context", ""),
                            recent_ideas=getattr(args, "recent_ideas", ()))
        result = flow.create_proposal(**proposal, trend_notes=args.trend)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    state = flow.read(args.id)
    folder = flow.root / args.id
    folder.mkdir(exist_ok=True)
    with (folder / "project.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = flow.read(args.id)
        if args.action == "show":
            print(json.dumps(state, indent=2, ensure_ascii=False))
            return
        if args.action == "footage":
            for clip in args.clips:
                if not any(s["codec_type"] == "video" for s in probe(Path(clip).resolve())["streams"]):
                    raise ValueError(f"Aucune piste vidéo dans {clip}")
            flow.approve_footage(args.id, args.clips, actor=args.actor)
        elif args.action == "voice":
            flow._advance(args.id, "footage_approved", "voice_generated")
            for clip in state["footage"]["files"]:
                verify(clip)
            output = folder / f"voix-{uuid4().hex[:8]}.wav"
            synthesize(state["proposal"]["script"], output, args.reference)
            flow.record_voice(args.id, output)
            print(output)
        elif args.action == "mixes":
            flow._advance(args.id, "voice_generated", "voice_generated")
            verify(state["voice"])
            state["mixes"] = previews(state["voice"], folder)
            flow._save(state)
            for name, mix in state["mixes"].items():
                print(f"{name} · {mix['bpm']} BPM · {mix['path']}")
        elif args.action == "listen-ok":
            music = state.get("mixes", {}).get(args.music)
            if args.music and (not music or music["voice_sha256"] != state["voice"]["sha256"]):
                raise ValueError("Préparez les préécoutes de cette voix avec mixes.")
            if music:
                verify(music)
            flow.approve_voice(args.id, actor=args.actor, music=music)
        elif args.action == "render":
            flow._advance(args.id, "voice_approved", "assembled")
            clips = state["footage"]["files"]
            for item in [*clips, state["voice"]]:
                verify(item)
            output = folder / "montage.mp4"
            music = state["voice"].get("music")
            if music:
                verify(music)
            timeline = render([item["path"] for item in clips], state["voice"]["path"], output,
                              state["proposal"]["script"], music)
            flow.record_assembly(args.id, output, timeline=timeline)
            print(output)
        elif args.action == "export-ok":
            flow.approve_export(args.id, actor=args.actor)
        elif args.action == "retake":
            if state["stage"] == "export_approved":
                raise ValueError("Export finalisé. Créez un nouveau projet pour cette version.")
            state.update(stage="proposed", footage=None, voice=None, mixes={}, assembly=None, final_review=None)
            flow._event(state, "retake_requested", args.actor, {})
            flow._save(state)
        print(flow.read(args.id)["stage"])


def guided(args):
    def step(action, **fields):
        return run(argparse.Namespace(root=args.root, action=action, **fields))

    project = step("idea", direction=args.direction, trend=args.trend)
    ident = project["id"]
    print(f"Projet {ident}. Tu peux interrompre avec Ctrl-C et reprendre via les commandes du README.")
    actor = input("Ton prénom : ").strip()
    clips = []
    while True:
        entry = input("Chemin d’un plan filmé (Entrée pour terminer) : ").strip()
        if not entry:
            break
        clips.append(str(Path(entry).expanduser()))
    if input("As-tu visionné et validé ces prises ? [oui/N] ").strip().lower() != "oui":
        return
    step("footage", id=ident, clips=clips, actor=actor)
    step("voice", id=ident, reference=args.reference)
    step("mixes", id=ident)
    choice = input("Écoute les préécoutes et choisis A, B ou Entrée pour la voix seule : ").strip().upper()
    if choice not in {"A", "B", ""}:
        raise ValueError("Choix inconnu. Reprends avec listen-ok après écoute.")
    if input("Écoute le WAV choisi. Valides-tu cette narration ? [oui/N] ").strip().lower() != "oui":
        return
    step("listen-ok", id=ident, actor=actor, music=choice)
    step("render", id=ident)
    if input("Regarde le MP4 affiché. Valides-tu l’export ? [oui/N] ").strip().lower() == "oui":
        step("export-ok", id=ident, actor=actor)


def main():
    parser = argparse.ArgumentParser(description="Créer un Reel narré en français.")
    parser.add_argument("--root", default="output/projects")
    commands = parser.add_subparsers(dest="action", required=True)
    studio = commands.add_parser("studio", help="Ouvrir le studio web local")
    studio.add_argument("--port", type=int, default=8765)
    idea = commands.add_parser("idea", help="Générer une idée, deux plans et une narration")
    idea.add_argument("direction", nargs="?", default="Un geste simple du quotidien")
    idea.add_argument("--trend", default="")
    guide = commands.add_parser("start", help="Suivre le parcours guidé jusqu’au MP4")
    guide.add_argument("direction", nargs="?", default="Un geste simple du quotidien")
    guide.add_argument("--trend", default="")
    guide.add_argument("--reference")
    for name in ("show", "footage", "voice", "mixes", "listen-ok", "render", "export-ok", "retake"):
        sub = commands.add_parser(name)
        sub.add_argument("id")
        if name == "listen-ok":
            sub.add_argument("--music", choices=["A", "B"])
        if name == "footage":
            sub.add_argument("clips", nargs="+")
        if name == "voice":
            sub.add_argument("--reference", help="Voix de référence personnelle facultative")
        if name in {"footage", "listen-ok", "export-ok", "retake"}:
            sub.add_argument("--actor", required=True)
    try:
        args = parser.parse_args()
        if args.action == "studio":
            import uvicorn
            from .studio import create_app
            uvicorn.run(create_app(args.root), host="127.0.0.1", port=args.port)
        elif args.action == "start":
            guided(args)
        else:
            run(args)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
