"""Sourced assistant proposals before owner review; bounded public collection only.

Plans are editorially curated extraction recipes, not a general web search engine.
Every quote must occur in an intact local Source. Reuse never redates a consultation.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from app.models.source import Source, SourceType
from app.services.ecosystem_http import PublicWeb
from app.services.ecosystem_parse import PageParser, canonical_url

STATUSES = {"proposed", "partial", "contradictory", "missing", "historical", "not_relevant"}
MAX_AGE = {"identity_location": 90, "access_booking": 7, "menu_prices": 14,
           "experience": 365, "history_people": 365, "opening_program": 7}


def normalized(text):
    return re.sub(r"\s+", " ", text).strip()


def plan_for(workflow, place_id):
    path = Path(workflow.read()["repository"]) / "brands/example/factual-sources.json"
    return json.loads(path.read_text()).get(place_id, {}) if path.exists() else {}


def document_for(workflow, source):
    state = workflow.read()
    if workflow.source_status(state, source) != "unchanged":
        raise ValueError("Preuve absente ou modifiée : " + source["id"])
    return json.loads((Path(state["repository"]) / source["path"]).read_text())


def acquire(workflow, place_id, urls, *, client=None, refresh=False):
    client = client or PublicWeb(max_requests=36, timeout=15)
    attempts = []
    for url in list(dict.fromkeys(urls))[:8]:
        attempted_at = workflow.clock().isoformat()
        try:
            url = canonical_url(url)
            sources = workflow.read()["places"][place_id]["sources"]
            existing = next((s for s in reversed(sources) if s["url"] == url
                             and workflow.source_status(workflow.read(), s) == "unchanged"), None)
            if existing and not refresh:
                attempts.append({"url": url, "status": "reused", "source_id": existing["id"],
                                 "consulted_at": existing["acquired_at"]})
                continue
            fetched = client.fetch(url)
            parser = PageParser(fetched["url"])
            parser.feed(fetched["raw"])
            if parser.restricted:
                raise ValueError("Page marquée noindex : acquisition refusée.")
            text = normalized(" ".join(parser.text))
            if not text:
                raise ValueError("Page sans texte exploitable.")
            now = workflow.clock().isoformat()
            source = Source(brand_id=workflow.read()["brand_id"], type=SourceType.URL,
                            title=normalized(" ".join(parser.title)) or fetched["url"],
                            url_or_path=fetched["url"], raw_content=fetched["raw"], extracted_content=text,
                            created_at=now, updated_at=now,
                            metadata={"origin": "assistant_dossier_research", "requested_url": url,
                                      "timestamp_meaning": "Consultation HTTP réelle, pas date de mise à jour du lieu.",
                                      "owner_verified": False})
            workflow.store.save_source(source)
            relative = workflow.store.path(f"sources/{source.id}.json").relative_to(Path(workflow.read()["repository"]))
            workflow.attach_source(place_id, {"id": source.id, "url": source.url_or_path,
                                             "acquired_at": now, "path": str(relative),
                                             "family": "public_document", "requested_url": url,
                                             "independence": "Voir l'éditeur ; un site officiel est une source opérateur."})
            attempts.append({"url": url, "status": "acquired", "source_id": source.id, "consulted_at": now})
        except Exception as error:
            attempts.append({"url": url, "status": "unavailable", "attempted_at": attempted_at,
                             "error": str(error)})
    return attempts


def prepare(workflow, place_id, *, refresh=False, client=None, plan=None):
    """Append a proposal snapshot, never modify choices, checks or project versions."""
    plan = plan if plan is not None else plan_for(workflow, place_id)
    attempts = acquire(workflow, place_id, plan.get("pages", []), client=client, refresh=refresh)
    place = workflow.read()["places"][place_id]
    points = {}
    for point in place["points"]:
        recipe = plan.get("points", {}).get(point, {})
        claims, issues = [], list(recipe.get("issues", []))
        for item in recipe.get("claims", []):
            source = next((s for s in reversed(place["sources"])
                           if s["url"] == item["url"] or s.get("requested_url") == item["url"]), None)
            if not source:
                issues.append("Source non acquise : " + item["url"])
                continue
            try:
                document = document_for(workflow, source)
                excerpt = normalized(item["excerpt"])
                if not excerpt or excerpt not in normalized(document.get("extracted_content", "")):
                    raise ValueError("Extrait justificatif introuvable dans la preuve : " + source["url"])
                claims.append({"information": item["information"], "excerpt": excerpt,
                               "source_id": source["id"], "url": source["url"],
                               "consulted_at": source["acquired_at"], "published_at": item.get("published_at"),
                               "source_kind": item.get("source_kind", "third_party"),
                               "historical": item.get("historical", False)})
            except (ValueError, OSError) as error:
                issues.append(str(error))
        status = recipe.get("status", "proposed" if claims else "missing")
        if status not in STATUSES:
            raise ValueError("Statut de recherche invalide.")
        if not claims and status not in {"missing", "not_relevant"}:
            status = "missing"
        elif issues and status == "proposed":
            status = "partial"
        if not recipe:
            issues.append("Recherche assistant à compléter pour ce point ; aucune information proposée.")
        points[point] = {"status": status, "claims": claims, "issues": issues,
                         "note": recipe.get("note", ""), "owner_verified": False}
    payload = {"place_id": place_id, "points": points, "attempts": attempts,
               "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
               "paid_calls": 0, "owner_verified": False}
    with workflow.transaction() as state:
        previous = next((e for e in reversed(state["history"]) if e["kind"] == "dossier_researched"
                         and e["payload"]["place_id"] == place_id), None)
        # Reopening cached research is idempotent. Failed refreshes remain explicit attempts.
        if previous and previous["payload"] == payload:
            return previous
        return workflow.event(state, "dossier_researched", "assistant", "assistant", payload)


def view(workflow, state, place):
    event = next((e for e in reversed(state["history"]) if e["kind"] == "dossier_researched"
                  and e["payload"]["place_id"] == place["id"]), None)
    now = workflow.now(state)
    points = {}
    for point in place["points"]:
        record = json.loads(json.dumps(event["payload"]["points"].get(point, {}))) if event else {}
        record = record or {"status": "missing", "claims": [], "issues": ["Recherche assistant non préparée."], "note": ""}
        record["issues"] = list(record["issues"])
        stale, intact = False, True
        for claim in record["claims"]:
            source = next((s for s in place["sources"] if s["id"] == claim["source_id"]), None)
            claim["integrity"] = workflow.source_status(state, source) if source else "missing"
            if claim["integrity"] != "unchanged":
                intact = False
                record["issues"].append("Preuve absente/modifiée : " + claim["url"])
            consulted = datetime.fromisoformat(claim["consulted_at"])
            claim["stale"] = now - consulted > timedelta(days=MAX_AGE.get(point, 30))
            if claim["stale"]:
                stale = True
                record["issues"].append(f"Consultation trop ancienne pour ce point ({MAX_AGE.get(point, 30)} jours) : {claim['url']}")
        record["stale"] = stale
        record["confirmable"] = bool(record["claims"]) and intact and not stale and record["status"] == "proposed"
        record["owner_verified"] = False
        points[point] = record
    return {"id": event["id"] if event else None, "prepared_at": event["recorded_at"] if event else None,
            "points": points, "attempts": event["payload"].get("attempts", []) if event else [],
            "role": "assistant", "owner_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/example/editorial-workflow")
    parser.add_argument("--place")
    parser.add_argument("--all-calibration", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    from app.services.editorial_workflow import EditorialWorkflow
    workflow = EditorialWorkflow(args.output)
    places = [p["id"] for p in workflow.read()["places"].values() if p["origin"] == "owner_calibration"] if args.all_calibration else [args.place]
    if not all(places):
        parser.error("--place ou --all-calibration requis")
    for place_id in places:
        event = prepare(workflow, place_id, refresh=args.refresh)
        print(json.dumps({"place": place_id, "research_id": event["id"],
                          "points": {k: v["status"] for k, v in event["payload"]["points"].items()}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
