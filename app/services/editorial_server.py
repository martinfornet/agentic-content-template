"""Loopback-only local operator interface. No paid calls or automatic publication."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import parse_qs, urlparse

from app.services.editorial_calibration import import_calibration
from app.services.editorial_workflow import EditorialWorkflow
from app.services.reel_pipeline import ReelPipeline, QA_POINTS
from app.services.visual_sourcing import research, select


class OperatorServer(ThreadingHTTPServer):
    def __init__(self, address, workflow, *, public_origin=None, remote_review=False):
        super().__init__(address, OperatorHandler)
        self.public_origin = public_origin
        self.remote_review = remote_review
        token_file=os.environ.get("AC_WORKER_TOKEN_FILE")
        self.worker_token=Path(token_file).read_text().strip() if token_file else None
        self.workflow = workflow
        self.pipeline = ReelPipeline(workflow)
        operator_file=os.environ.get("AC_OPERATOR_TOKEN_FILE")
        self.token = Path(operator_file).read_text().strip() if operator_file else secrets.token_urlsafe(32)
        self.jobs = ThreadPoolExecutor(max_workers=1)
        from app.services.studio_delivery import recover_deliveries
        recover_deliveries(workflow)
        self.pipeline.recover_interrupted()
        from app.services.review_state import ReviewState
        self.review_state=ReviewState(workflow,self.pipeline)

    def server_close(self):
        if hasattr(self, "jobs"):
            self.jobs.shutdown(wait=False, cancel_futures=True)
        super().server_close()


class OperatorHandler(BaseHTTPRequestHandler):
    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from app.services.studio_delivery import delivery_lock
        with delivery_lock(self.server.workflow):return self.read_GET()

    def read_GET(self):
        if not self.valid_host():
            return self.json_response({"error": "Hôte local requis."}, 403)
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/review"}:
                html = (Path(__file__).parents[1] / ("web/review.html" if parsed.path == "/review" or self.server.remote_review else "web/editorial.html")).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self' blob:; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(html)
            elif parsed.path == '/api/review/updates':
                self.json_response(self.server.review_state.updates())
            elif parsed.path == '/comparison':
                ident = parse_qs(parsed.query).get('id', [''])[0]
                if not 1 <= len(ident) <= 80 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in ident):
                    raise ValueError('Identifiant de comparaison invalide.')
                html = self.server.pipeline.store.path('comparisons/' + ident + '.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(html)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; media-src 'self'; frame-ancestors 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(html)
            elif parsed.path == '/api/review':
                self.json_response(dict(self.server.review_state.summary(),token=self.server.token,remote_review=self.server.remote_review))
            elif parsed.path == '/api/review/detail':
                q=parse_qs(parsed.query)
                self.json_response(self.server.review_state.detail(q['kind'][0],q['id'][0]))
            elif parsed.path == '/api/production-chain':
                from app.services.production_chain import ProductionChain
                place=parse_qs(parsed.query)['place'][0]
                if place not in self.server.workflow.read()['places']:raise ValueError('Lieu inconnu.')
                self.json_response(ProductionChain(self.server.workflow).detail(place))
            elif parsed.path == "/api/state":
                workflow = self.server.workflow
                day = workflow.daily()
                state = workflow.read()
                projects = []
                for path in self.server.pipeline.store.root.glob("projects/*/project.json"):
                    project = self.server.pipeline.read(path.parent.name)
                    project["visual_lot"] = self.server.pipeline.visual_lot(project)
                    projects.append(project)
                from app.services.reel_story import StoryPreparation, place_packet
                stories = StoryPreparation(workflow)
                prepared = [v for ident in state['places'] if (v := stories.latest(ident)) is not None]
                readiness = []
                for ident in state['places']:
                    packet = place_packet(workflow, ident)
                    readiness.append({'place_id': ident, 'readiness': packet['readiness'],
                                      'external_document_count': packet['external_document_count']})
                from app.services.editorial_inbox import EditorialInbox
                from app.services.reel_jobs import ReelJobs
                from app.services.reel_render_requests import ReelRenderRequests
                jobs=ReelJobs(workflow,self.server.pipeline)
                from app.services.studio_tasks import StudioTasks
                immediate=StudioTasks(workflow)
                from app.services.reel_covers import ReelCovers
                from app.services.reel_publications import ReelPublications
                self.json_response({"publications":ReelPublications(workflow,self.server.pipeline).list(),"covers":ReelCovers(workflow,self.server.pipeline).list(),"immediate_tasks":immediate.list(),"immediate_engine":immediate.store.read("engine.json",{}),"render_requests":ReelRenderRequests(workflow).list(),"music_plans":music_plans_for_review(workflow),"visual_audits":workflow.store.read("visual-audits.json",{}),"feedback_receipts":workflow.store.read("feedback-receipts.json",{}),"jobs":jobs.list(),"worker":jobs.status(),"remote_review": self.server.remote_review, "inbox_reviews": EditorialInbox(workflow, self.server.pipeline).list(), "editorial_memory":{ident:place_packet(workflow,ident)["editorial_memory"] for ident in state["places"]},"prepared_stories": prepared, "writing_readiness": readiness, "token": self.server.token, "owner": state["owner_id"], "day": day,
                                    "dossiers": [workflow.dossier(p) for p in state["places"]], "projects": projects,
                                    "qa_points": QA_POINTS, "preflight": self.server.pipeline.preflight()})
            elif parsed.path == "/files":
                query = parse_qs(parsed.query)
                relative = query["path"][0]
                path = self.server.pipeline.store.path(relative)
                if path.suffix.lower() not in {".mp4", ".mov", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".srt", ".json", ".wav", ".mp3", ".zip"}:
                    raise ValueError("Type de fichier non accessible.")
                self.serve_file(path, "download" in query)
            elif parsed.path == "/api/evidence":
                query = parse_qs(parsed.query)
                state = self.server.workflow.read()
                place = state["places"][query["place"][0]]
                source = next(s for s in place["sources"] if s["id"] == query["source"][0])
                if self.server.workflow.source_status(state, source) != "unchanged":
                    raise ValueError("Preuve absente ou modifiée.")
                document = json.loads((Path(state["repository"]) / source["path"]).read_text())
                self.json_response({"source": source, "text": document.get("extracted_content") or document.get("raw_content", "")})
            else:
                self.json_response({"error": "Route inconnue."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers cancel metadata/range requests when changing previews.
            # The response has already started; never append a JSON error to it.
            return
        except (ValueError, KeyError, OSError, StopIteration, TypeError) as error:
            self.json_response({"error": str(error)}, 400)

    def serve_file(self, path, download=False):
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        header = self.headers.get("Range")
        if header:
            import re
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
            if not match:
                raise ValueError("Plage vidéo invalide.")
            start = int(match[1])
            end = min(int(match[2]) if match[2] else end, end)
            if start > end:
                self.send_response(416)
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        if urlparse(self.path).path in {"/api/worker", "/api/worker-upload"}:
            return self.worker_action()
        origin = self.headers.get("Origin")
        allowed_origin = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if getattr(self.server, "public_origin", None):
            allowed_origin.add(self.server.public_origin)
        if not self.valid_host() or (origin and origin not in allowed_origin):
            return self.json_response({"error": "Origine de connexion non autorisée.", "code":"origin_rejected"}, 403)
        if self.headers.get("X-Operator-Token") != self.server.token:
            return self.json_response({"error":"La session du studio a été actualisée. Votre saisie est conservée ; réessayez.","code":"operator_token_changed"},403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100 * 1024 * 1024:
                raise ValueError("Taille de requête invalide (maximum 100 Mo).")
            parsed = urlparse(self.path)
            if getattr(self.server, "remote_review", False) and parsed.path not in {"/api/inbox", "/api/production", "/api/jobs", "/api/publications", "/api/tasks/retry", "/api/memory", "/api/quality", "/api/performance", "/api/operating"}:
                raise ValueError("Cette action de production attend la connexion du Mac. Vos corrections et retours peuvent être enregistrés dans le studio.")
            workflow, pipeline = self.server.workflow, self.server.pipeline
            actor = workflow.read()["owner_id"]
            if parsed.path == "/api/upload":
                query = parse_qs(parsed.query)
                result = pipeline.upload(query["choice"][0], int(query["beat"][0]), query["name"][0],
                                         self.rfile.read(length), actor, query["provenance"][0])
            else:
                data = json.loads(self.rfile.read(length))
                owner = {"actor": actor, "role": "owner"}
                if parsed.path == '/api/production':
                    from app.services.studio_tasks import StudioTasks
                    result=StudioTasks(workflow).request_production(data['place'],data['instruction'],origin='studio',request_key=data['request_key'])
                elif parsed.path == '/api/tasks/retry':
                    from app.services.studio_tasks import StudioTasks
                    result=StudioTasks(workflow).retry(data['id'])
                elif parsed.path == '/api/memory':
                    from app.services.editorial_memory import update_feedback_memory
                    result=update_feedback_memory(workflow,data['review_id'],data['scope'],data.get('active',True))
                elif parsed.path == '/api/quality':
                    from app.services.quality_dossier import QualityDossier
                    result=QualityDossier(workflow,pipeline).record(data['kind'],data['target_id'],data['version'],data['section'],data['entries'])
                elif parsed.path == '/api/performance':
                    from app.services.studio_performance import record_performance
                    result=record_performance(workflow,pipeline,data)
                elif parsed.path == '/api/operating':
                    places=data.get('priority_places',[]);limit=data.get('active_limit',3)
                    if type(limit) is not int or not 1<=limit<=10 or not isinstance(places,list) or len(set(places))!=len(places) or any(p not in workflow.read()['places'] for p in places):raise ValueError('Priorités invalides.')
                    result={'active_limit':limit,'priority_places':places};workflow.store.write('studio-operating.json',result)
                elif parsed.path == "/api/jobs":
                    from app.services.reel_jobs import ReelJobs
                    result=ReelJobs(workflow,pipeline).enqueue(data["kind"],data["story_id"],data.get("tempo",1.03))
                elif parsed.path == "/api/publications":
                    from app.services.reel_publications import ReelPublications
                    result = ReelPublications(workflow,pipeline).mark(data["run_id"],data.get("published",True),metadata=data.get("metadata"))
                elif parsed.path == "/api/inbox":
                    from app.services.editorial_inbox import EditorialInbox
                    result = EditorialInbox(workflow,pipeline).submit(data["kind"],data["target_id"],data["decision"],data.get("note",""),texts=data.get("texts"),caption=data.get("caption"),memory_scope=data.get("memory_scope","auto"),audition_sha=data.get("audition_sha"),music_choice=data.get("music_choice"),correction=data.get("correction"))
                elif parsed.path == "/api/interest":
                    result = workflow.review(data["place"], data["decision"], data["note"], reference="interface locale — revue explicite", **owner)
                elif parsed.path == "/api/choose":
                    workflow.daily()
                    result = workflow.choose(data["place"], data["note"], **owner)
                elif parsed.path == "/api/cancel":
                    result = workflow.cancel(data["note"], **owner)
                elif parsed.path == "/api/check":
                    result = workflow.check(data["choice"], data["point"], data["result"], data["note"], data["source"], data["checked_at"], **owner)
                elif parsed.path == "/api/draft":
                    result = pipeline.draft(data["choice"])
                elif parsed.path == "/api/edit":
                    result = pipeline.edit(data["choice"], data["draft"], actor)
                elif parsed.path == "/api/approve":
                    result = pipeline.approve(data["choice"], actor, data["note"])
                elif parsed.path == "/api/produce":
                    result = pipeline.start_run(data["choice"])
                    if result["status"] == "queued":
                        self.server.jobs.submit(pipeline.render, data["choice"], result["id"])
                elif parsed.path == "/api/preview":
                    result = pipeline.start_preview(data["choice"], actor, data["instruction"])
                    if result["status"] == "queued":
                        self.server.jobs.submit(pipeline.render, data["choice"], result["id"])
                elif parsed.path == "/api/source":
                    project = pipeline.read(data["choice"])
                    plans = json.loads((pipeline.repository / "brands/example/visual-sources.json").read_text())
                    pages = plans.get(project["place_id"], [])
                    if not pages:
                        raise ValueError("Plan visuel de ce lieu à compléter par l'assistant ; aucun remplissage générique.")
                    with pipeline.transaction(data["choice"]) as current:
                        if current.get("sourcing_status") in {"queued", "running"}:
                            raise ValueError("Recherche déjà en cours.")
                        current["sourcing_status"] = "queued"
                    self.server.jobs.submit(self.source_job, data["choice"], pages)
                    result = {"status": "queued", "paid_calls": 0}
                elif parsed.path == "/api/visual_select":
                    result = select(pipeline, data["choice"], data["candidate"], int(data["beat"]), data["note"], actor=actor, role="owner")
                elif parsed.path == "/api/visual_lot_review":
                    result = pipeline.review_visual_lot(data["choice"], actor, data["note"], data["passed"], data["signature"])
                elif parsed.path == "/api/visual_lot_package":
                    result = pipeline.package_visual_lot(data["choice"])
                elif parsed.path == "/api/qa":
                    result = pipeline.human_qa(data["choice"], data["run"], actor, data["checks"], data["note"], data["passed"])
                elif parsed.path == "/api/export":
                    result = pipeline.export(data["choice"], data["run"], actor)
                else:
                    return self.json_response({"error": "Route inconnue."}, 404)
            self.json_response(result)
        except (ValueError, KeyError, OSError, TypeError, StopIteration, subprocess.SubprocessError) as error:
            self.json_response({"error": str(error)}, 400)

    def worker_action(self):
        expected=getattr(self.server,"worker_token",None)
        provided=self.headers.get("X-Worker-Token","")
        if not self.valid_host() or not expected or not secrets.compare_digest(expected,provided):
            return self.json_response({"error":"Moteur non autorisé."},403)
        try:
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=100*1024*1024:raise ValueError("Taille de résultat invalide.")
            from app.services.reel_jobs import ReelJobs
            from app.services.editorial_inbox import EditorialInbox
            jobs=ReelJobs(self.server.workflow,self.server.pipeline)
            parsed=urlparse(self.path)
            if parsed.path=="/api/worker-upload":
                q=parse_qs(parsed.query)
                result=jobs.upload(q['job'][0],self.headers.get('X-Job-Lease',''),q['name'][0],self.rfile.read(length))
            else:
                data=json.loads(self.rfile.read(length));action=data['action']
                if action=='sync':
                    from app.services.reel_story import StoryPreparation
                    stories=StoryPreparation(self.server.workflow)
                    policy=self.server.workflow.store.read('editorial-memory/policy.json',{'rules':[]})
                    result={'reviews':EditorialInbox(self.server.workflow,self.server.pipeline).list(),'stories':[v for ident in self.server.workflow.read()['places'] if (v:=stories.latest(ident)) and v.get('author')=='owner'],'policy':policy}
                elif action=='ack_feedback':
                    from datetime import datetime,UTC
                    ids=data.get('reviews',[])
                    known={r['id'] for r in EditorialInbox(self.server.workflow,self.server.pipeline).list()}
                    if not isinstance(ids,list) or any(not isinstance(i,str) or i not in known for i in ids):raise ValueError('Retour inconnu.')
                    receipts=self.server.workflow.store.read('feedback-receipts.json',{})
                    for ident in ids:receipts.setdefault(ident,{'received_by_mac_at':datetime.now(UTC).isoformat()})
                    self.server.workflow.store.write('feedback-receipts.json',receipts)
                    result={'ok':True}
                elif action=='wait_feedback':
                    from app.services.studio_events import wait_feedback
                    result=wait_feedback(self.server.workflow,data.get('cursor'))
                elif action=='claim':result={'job':jobs.claim()}
                elif action=='claim_approved':
                    from app.services.reel_render_requests import ReelRenderRequests
                    result={'job':ReelRenderRequests(self.server.workflow).claim_approved(self.server.workflow,self.server.pipeline)}
                elif action=='heartbeat':result=jobs.heartbeat(data.get('job'),data.get('lease'))
                elif action=='finish':result=jobs.finish(data['job'],data['lease'],data['result'])
                else:raise ValueError('Action moteur inconnue.')
            self.json_response(result)
        except (ValueError,KeyError,OSError,TypeError) as error:
            self.json_response({'error':str(error)},400)

    def source_job(self, choice, pages):
        pipeline = self.server.pipeline
        try:
            with pipeline.transaction(choice) as project:
                project["sourcing_status"] = "running"
            research(pipeline, choice, pages)
            with pipeline.transaction(choice) as project:
                project["sourcing_status"] = "completed"
        except Exception as error:
            with pipeline.transaction(choice) as project:
                project["sourcing_status"] = "failed"
                pipeline.event(project, "visual_research_failed", "assistant", {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/example/editorial-workflow")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    workflow = EditorialWorkflow(args.output)
    workflow.read()
    imported = import_calibration(workflow)
    server = OperatorServer(("127.0.0.1", args.port), workflow)
    print(f"Interface locale : http://127.0.0.1:{server.server_port} — calibration importée : {len(imported)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def music_plans_for_review(workflow):
    from app.services.reel_pipeline import digest
    plans=workflow.store.read('music-plans.json',{})
    from app.services.reel_music_usage import unavailable,usage,reservations
    used,reserved=usage(workflow),reservations(workflow)
    for place,plan in plans.items():
        available,blocked=[],[]
        for candidate in plan.get('candidates',[]):
            reason=unavailable(workflow,candidate,place,used=used,reserved=reserved)
            if reason:blocked.append({'title':candidate.get('title'),'reason':reason})
            else:
                candidate['version']=digest(candidate)
                available.append(candidate)
        plan.update(candidates=available,unavailable_music=blocked)
    return plans


if __name__ == "__main__":
    main()
