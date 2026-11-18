"""Loopback web studio for the same creator actions used by the CLI."""
from argparse import Namespace
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from .workflow import CreatorWorkflow
from .morning import Morning


def create_app(root):
    from .__main__ import run

    flow = CreatorWorkflow(root)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    lock = Lock()
    web = Path(__file__).with_name("web")

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method == "POST" and (
            request.headers.get("x-creator-studio") != "1" or
            request.headers.get("origin") != f"http://{request.headers.get('host')}"
        ):
            return JSONResponse({"detail": "Requête extérieure au studio."}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(subprocess.SubprocessError)
    async def tool_error(request, exc):
        return JSONResponse({"detail": "Le moteur a interrompu cette étape. Consulte le terminal du studio puis réessaie."}, status_code=422)

    def state(id):
        try:
            return flow.read(id)
        except ValueError as exc:
            raise HTTPException(404, "Projet introuvable.") from exc

    def view(p):
        return {k: p[k] for k in ("id", "proposal", "stage", "updated_at")} | {
            "choices": list((p.get("mixes") or {}).keys()) if p["stage"] == "voice_generated" else [],
            "clips": [Path(c["path"]).name for c in (p.get("footage") or {}).get("files", [])],
        }

    @app.get("/")
    def home():
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.get("/api/projects")
    def projects():
        rows = [view(state(p.stem)) for p in flow.root.glob("*.json")]
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)

    profile = flow.root / "profil" / "contexte.txt"

    morning = Morning(root)

    @app.get("/api/morning")
    def morning_state():
        return morning.status()

    @app.post("/api/morning")
    def morning_change(enabled: bool = Form(...)):
        return morning.change(enabled)

    @app.get("/api/context")
    def current_context():
        return {"text": profile.read_text(encoding="utf-8") if profile.exists() else ""}

    @app.post("/api/context")
    def save_context(text: str = Form(...)):
        value = text.strip()
        if not value or len(value) > 24000:
            raise ValueError("Le contexte doit contenir entre 1 et 24 000 caractères.")
        with lock:
            profile.parent.mkdir(exist_ok=True)
            temp = profile.with_name(f"{uuid4().hex}.tmp")
            temp.write_text(value, encoding="utf-8")
            temp.replace(profile)
        return {"text": value}

    @app.post("/api/projects")
    def new(direction: str = Form(""), trend: str = Form("")):
        context = current_context()["text"]
        if not direction.strip() and not context:
            raise ValueError("Importe d’abord la présentation de ton entreprise.")
        if len(trend) > 3000:
            raise ValueError("Décris la tendance en 3 000 caractères maximum.")
        with morning.lock():
            prior = sorted(flow.root.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:12]
            ideas = [state(f.stem)["proposal"]["idea"] for f in prior]
            p = run(Namespace(action="idea", root=root, direction=direction.strip(), trend=trend,
                              context=context, recent_ideas=ideas))
            return view(p)

    def upload(folder, item):
        name = Path(item.filename or "prise").name
        if name in {".", "..", ""}:
            raise ValueError("Nom de fichier invalide.")
        dest = folder / name
        with dest.open("xb") as out:
            shutil.copyfileobj(item.file, out)
        return str(dest)

    @app.post("/api/projects/{id}/{action}")
    def action(id: str, action: str, version: str = Form(...), actor: str = Form(""),
               music: str = Form(""), clips: list[UploadFile] = File(default=[]),
               reference: UploadFile | None = File(default=None)):
        with lock:
            p = state(id)
            if p["updated_at"] != version:
                raise HTTPException(409, "Le projet a évolué. Recharge le studio.")
            if action not in {"footage", "voice", "mixes", "listen-ok", "render", "export-ok", "retake"}:
                raise HTTPException(404)
            args = Namespace(root=root, id=id, action=action, actor=actor.strip(), music=music, reference=None)
            folder = flow.root / id
            folder.mkdir(exist_ok=True)
            if action == "footage":
                batch = Path(tempfile.mkdtemp(dir=folder, prefix="prises-"))
                try:
                    args.clips = []
                    for i, item in enumerate(clips):
                        slot = batch / str(i+1)
                        slot.mkdir()
                        args.clips.append(upload(slot, item))
                    run(args)
                except Exception:
                    shutil.rmtree(batch)
                    raise
            elif action == "voice":
                with tempfile.TemporaryDirectory(dir=folder) as tmp:
                    if reference:
                        args.reference = upload(Path(tmp), reference)
                    run(args)
            else:
                run(args)
            return view(state(id))

    @app.get("/media/{id}/{kind}")
    def media(id: str, kind: str, download: bool = False):
        p = state(id)
        sources = {}
        if p.get("voice"):
            sources["voice"] = p["voice"]
            sources.update(p.get("mixes") or {})
        if p.get("assembly"):
            sources["video"] = p["assembly"]
        record = sources.get(kind)
        if not record or (download and (kind != "video" or p["stage"] != "export_approved")):
            raise HTTPException(404)
        from .__main__ import verify
        verify(record)
        return FileResponse(record["path"], media_type="video/mp4" if kind == "video" else "audio/wav",
                            filename="montage.mp4" if download else None,
                            headers={"Cache-Control": "no-store"})

    return app
