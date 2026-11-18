"""Atomic local ledger for reserve → daily J+2 choice → scoped owner checks.

No acquisition, inference, content generation or production authorization.
The local role is an operator declaration, not an authentication system.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.editorial_workflow import ReservePlace
from app.services.acquisition_store import AcquisitionStore


class EditorialWorkflow:
    def __init__(self, root, *, clock=None):
        self.store = AcquisitionStore(root)
        self.clock = clock or (lambda: datetime.now(ZoneInfo("UTC")))

    @contextmanager
    def transaction(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        with self.store.path("workflow.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.store.read("workflow.json")
            yield state
            if state is not None:
                self.store.write("workflow.json", state)

    def now(self, state):
        return self.clock().astimezone(ZoneInfo(state["timezone"]))

    def event(self, state, kind, actor, role, payload):
        if role not in {"owner", "assistant", "system"} or not actor.strip():
            raise ValueError("Auteur et rôle explicites requis.")
        event = {"id": uuid4().hex, "kind": kind, "actor": actor, "role": role,
                 "recorded_at": self.now(state).isoformat(), "payload": payload}
        state["history"].append(event)
        return event

    def owner(self, state, actor, role):
        if role != "owner" or actor != state["owner_id"]:
            raise ValueError("Action réservée au propriétaire déclaré dans le registre.")

    def initialize(self, seed, repository, owner_id="proprietaire"):
        # Validate everything before writing; never alter original acquisition stores.
        repository = Path(repository).resolve()
        places = [ReservePlace(**p).to_dict() for p in seed["places"]]
        if len({p["id"] for p in places}) != len(places) or len({p["identity"] for p in places}) != len(places):
            raise ValueError("Deux variantes d'un lieu ne sont pas deux identités.")
        ZoneInfo(seed["timezone"])
        if not owner_id.strip():
            raise ValueError("Propriétaire requis.")
        for place in places:
            for source in place["sources"]:
                path = (repository / source["path"]).resolve()
                if not path.is_relative_to(repository):
                    raise ValueError("La preuve doit rester dans le dépôt local.")
                if path.is_file():
                    document = json.loads(path.read_text())
                    if document["id"] != source["id"] or document["url_or_path"] != source["url"]:
                        raise ValueError("Référence Source incohérente.")
                source["sha256_at_import"] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        with self.transaction() as existing:
            if existing is not None:
                if existing["seed_id"] != seed["seed_id"] or existing["owner_id"] != owner_id:
                    raise ValueError("Registre existant : initialisation incompatible.")
                return existing
            state = {"schema_version": 1, "seed_id": seed["seed_id"], "brand_id": seed["brand_id"],
                     "timezone": seed["timezone"], "plan_fingerprint": seed["plan_fingerprint"],
                     "owner_id": owner_id, "repository": str(repository), "places": {p["id"]: p for p in places},
                     "history": [], "days": {}}
            self.event(state, "checkpoint_import", "checkpoint", "system",
                       {"seed_id": seed["seed_id"], "places": places,
                        "notice": "Décisions historiques importées ; aucune nouvelle décision, visite ou vérification."})
            self.store.write("workflow.json", state)
            return state

    def read(self):
        state = self.store.read("workflow.json")
        if state is None:
            raise ValueError("Initialiser le registre avec init.")
        return state

    def source_status(self, state, source):
        path = Path(state["repository"]) / source["path"]
        if not path.is_file():
            return "missing"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return "unchanged" if digest == source["sha256_at_import"] else "changed_or_not_imported"

    def add(self, value, *, actor, role, revise=False):
        place = ReservePlace(**value).to_dict()
        if place["owner_interest"] != "pending":
            raise ValueError("Un nouveau lieu entre avec arbitrage ouvert ; utiliser interest ensuite.")
        with self.transaction() as state:
            if role not in {"owner", "assistant"}:
                raise ValueError("Auteur owner ou assistant requis.")
            if role == "owner":
                self.owner(state, actor, role)
            previous = state["places"].get(place["id"])
            if revise and (not previous or previous["identity"] != place["identity"]):
                raise ValueError("Révision : id et identité du lieu existant requis.")
            if not revise and (previous or any(p["identity"] == place["identity"] for p in state["places"].values())):
                raise ValueError("Identité déjà présente : aucune fusion ni variante automatique.")
            for source in place["sources"]:
                path = (Path(state["repository"]) / source["path"]).resolve()
                if not path.is_relative_to(Path(state["repository"])) or not path.is_file():
                    raise ValueError("Preuve locale requise dans le dépôt.")
                document = json.loads(path.read_text())
                if document["id"] != source["id"] or document["url_or_path"] != source["url"]:
                    raise ValueError("Référence Source incohérente.")
                source["sha256_at_import"] = hashlib.sha256(path.read_bytes()).hexdigest()
            if revise:
                place["owner_interest"] = "rejected" if previous["owner_interest"] == "rejected" else "pending"
                place["owner_note"] = previous["owner_note"] if place["owner_interest"] == "rejected" else "Dossier révisé : nouvelle confirmation requise ; décisions antérieures dans l'historique."
                place["owner_review"] = previous["owner_review"] if place["owner_interest"] == "rejected" else {}
            state["places"][place["id"]] = place
            return self.event(state, "reserve_revised" if revise else "reserve_added", actor, role,
                              {"place_id": place["id"], "place": place, "previous": previous})

    def attach_source(self, place_id, source, *, actor="assistant"):
        """Append immutable evidence without replacing owner decisions or checks."""
        with self.transaction() as state:
            path = (Path(state["repository"]) / source["path"]).resolve()
            if actor != "assistant" or not path.is_relative_to(Path(state["repository"])) or not path.is_file():
                raise ValueError("Source locale et attribution assistant requises.")
            document = json.loads(path.read_text())
            if document["id"] != source["id"] or document["url_or_path"] != source["url"]:
                raise ValueError("Référence Source incohérente.")
            evidence = {**source, "sha256_at_import": hashlib.sha256(path.read_bytes()).hexdigest()}
            place = state["places"][place_id]
            existing = next((s for s in place["sources"] if s["id"] == source["id"]), None)
            if existing:
                if existing != evidence:
                    raise ValueError("Une Source existante est immuable.")
                return existing
            place["sources"].append(evidence)
            self.event(state, "evidence_added", actor, "assistant", {"place_id": place_id, "source": evidence, "owner_verified": False})
            return evidence

    def eligibility(self, state, place, target):
        reasons = []
        if place["owner_interest"] != "retained":
            reasons.append(f"Intérêt propriétaire {place['owner_interest']} : {place['owner_note']}")
        if not all(self.source_status(state, s) == "unchanged" for s in place["sources"]):
            reasons.append("Preuve locale absente ou modifiée : réexamen requis.")
        if place["timing"] == "event":
            if not place["end_date"] or target > datetime.fromisoformat(place["end_date"]).date():
                reasons.append("Événement expiré à J+2 ou fin inconnue.")
        if place["timing"] == "opening":
            if not place["opening_date"]:
                reasons.append("Date d'ouverture inconnue ; reclasser en durable après revue.")
            else:
                age = (target - datetime.fromisoformat(place["opening_date"]).date()).days
                if not 0 <= age <= 30:
                    reasons.append("Hors fenêtre ouverture à J+2 ; reclasser en durable après revue.")
        return reasons

    def daily(self):
        with self.transaction() as state:
            if state is None:
                raise ValueError("Initialiser le registre.")
            today = self.now(state).date()
            target = today + timedelta(days=2)
            proposals, excluded = [], {}
            for place in state["places"].values():
                reasons = self.eligibility(state, place, target)
                if reasons:
                    excluded[place["id"]] = reasons
                else:
                    proposals.append(json.loads(json.dumps(place)))
            # A reopened day is a new snapshot only if the underlying reserve changed.
            day = state["days"].get(today.isoformat())
            if day and day["proposals"] == proposals and day["excluded"] == excluded:
                return day
            event = self.event(state, "daily_proposals", "workflow", "system",
                               {"day": today.isoformat(), "publication_day": target.isoformat(),
                                "proposals": proposals, "excluded": excluded})
            previous_choice = day["choice"] if day else None
            preserved = None
            if previous_choice and len(proposals) >= 2:
                choice = next(e for e in state["history"] if e["id"] == previous_choice)
                place_id = choice["payload"]["place_id"]
                if place_id in {p["id"] for p in proposals} and choice["payload"]["interest_version"] == self.interest_version(state, place_id):
                    preserved = previous_choice
            day = {**event["payload"], "id": event["id"], "choice": preserved,
                   "gap": max(0, 2 - len(proposals)), "planned_posts": 1}
            state["days"][today.isoformat()] = day
            return day

    def review(self, place_id, decision, note, *, actor, role, reference):
        if not note.strip() or not reference.strip():
            raise ValueError("Motif et référence de revue obligatoires.")
        with self.transaction() as state:
            place = state["places"][place_id]
            if role == "owner":
                self.owner(state, actor, role)
                if decision not in {"retained", "deepen", "rejected", "pending"}:
                    raise ValueError("Décision d'intérêt invalide.")
                place["owner_interest"], place["owner_note"] = decision, note
            elif role == "assistant":
                if decision not in {"deepen", "needs_review", "reject"}:
                    raise ValueError("Un avis assistant ne confirme pas l'intérêt propriétaire.")
                place["assistant_review"] = {"decision": decision, "note": note,
                                             "plan_fingerprint": state["plan_fingerprint"], "reference": reference}
            else:
                raise ValueError("Revue owner ou assistant requise.")
            event = self.event(state, "interest_review", actor, role,
                               {"place_id": place_id, "decision": decision, "note": note, "reference": reference})
            review = {"actor": actor, "role": role, "recorded_at": event["recorded_at"], "reference": reference,
                      "decision": decision, "note": note}
            if role == "owner":
                place["owner_review"] = review
            else:
                place["assistant_review"].update(review)
            return event

    def choose(self, place_id, note, *, actor, role):
        if not note.strip():
            raise ValueError("Motif de choix requis.")
        with self.transaction() as state:
            self.owner(state, actor, role)
            today = self.now(state).date()
            day = state["days"].get(today.isoformat())
            if not day or day["gap"]:
                raise ValueError("Ouvrir À choisir avec au moins deux lieux distincts ; lacune à résoudre.")
            if place_id not in {p["id"] for p in day["proposals"]}:
                raise ValueError("Lieu absent des propositions du jour.")
            target = today + timedelta(days=2)
            if self.eligibility(state, state["places"][place_id], target):
                raise ValueError("Lieu devenu inéligible ; actualiser À choisir.")
            event = self.event(state, "publication_choice", actor, role,
                               {"place_id": place_id, "day_id": day["id"], "publication_day": target.isoformat(),
                                "note": note, "interest_version": self.interest_version(state, place_id),
                                "points": dict(state["places"][place_id]["points"]),
                                "required_points": list(state["places"][place_id]["required_points"])})
            day["choice"] = event["id"]
            return event

    def cancel(self, note, *, actor, role):
        if not note.strip():
            raise ValueError("Motif d'annulation requis.")
        with self.transaction() as state:
            self.owner(state, actor, role)
            day = state["days"].get(self.now(state).date().isoformat())
            if not day or not day["choice"]:
                raise ValueError("Aucun choix aujourd'hui.")
            choice = next(e for e in state["history"] if e["id"] == day["choice"])
            event = self.event(state, "choice_cancelled", actor, role,
                               {"choice_id": day["choice"], "place_id": choice["payload"]["place_id"], "note": note})
            day["choice"] = None
            return event

    def check(self, choice_id, point, result, note, source, checked_at, *, actor, role):
        if result not in {"verified", "unresolved", "contradicted", "not_applicable"}:
            raise ValueError("Résultat de contrôle invalide.")
        if not all(v.strip() for v in (point, note, source, checked_at)):
            raise ValueError("Point, résultat, note, source et date de contrôle requis.")
        timestamp = datetime.fromisoformat(checked_at)
        if timestamp.tzinfo is None:
            raise ValueError("La date de contrôle doit préciser le fuseau.")
        with self.transaction() as state:
            self.owner(state, actor, role)
            choices = [e for e in state["history"] if e["id"] == choice_id and e["kind"] == "publication_choice"]
            if not choices or not any(d["choice"] == choice_id for d in state["days"].values()):
                raise ValueError("Choix absent, remplacé ou annulé.")
            choice = choices[0]
            if point not in choice["payload"]["points"]:
                raise ValueError("Point hors du périmètre de ce choix.")
            if result == "not_applicable" and point in choice["payload"]["required_points"]:
                raise ValueError("Ce point obligatoire doit être contrôlé, pas écarté.")
            if not datetime.fromisoformat(choice["recorded_at"]) <= timestamp <= self.now(state):
                raise ValueError("Contrôle requis après choix, sans date future.")
            publication = datetime.fromisoformat(choice["payload"]["publication_day"]).date()
            if self.now(state).date() > publication:
                raise ValueError("Créneau passé : aucune vérification rétroactive.")
            place = state["places"][choice["payload"]["place_id"]]
            if self.eligibility(state, place, publication) or choice["payload"]["interest_version"] != self.interest_version(state, place["id"]):
                raise ValueError("Intérêt retiré ou preuve changée : revoir le choix.")
            return self.event(state, "fact_check", actor, role,
                              {"choice_id": choice_id, "place_id": place["id"], "point": point,
                               "result": result, "note": note, "source": source, "checked_at": checked_at})

    def interest_version(self, state, place_id):
        reviews = [e["id"] for e in state["history"] if e["payload"].get("place_id") == place_id
                   and (e["kind"] == "reserve_revised" or (e["kind"] == "interest_review" and e["role"] == "owner"))]
        return reviews[-1] if reviews else state["history"][0]["id"]

    def dossier(self, place_id):
        state = self.read()
        place = state["places"][place_id]
        history = [e for e in state["history"] if e["payload"].get("place_id") == place_id
                   or (e["kind"] == "checkpoint_import" and any(p["id"] == place_id for p in e["payload"]["places"]))]
        choices = [e for e in history if e["kind"] == "publication_choice"]
        active = {d["choice"] for d in state["days"].values() if d["choice"]}
        summaries = []
        for choice in choices:
            latest = {e["payload"]["point"]: e["payload"] for e in history
                      if e["kind"] == "fact_check" and e["payload"]["choice_id"] == choice["id"]}
            pending = [p for p in choice["payload"]["points"]
                       if latest.get(p, {}).get("result") not in {"verified", "not_applicable"}]
            blockers = self.eligibility(state, place, datetime.fromisoformat(choice["payload"]["publication_day"]).date())
            if choice["id"] not in active:
                blockers.append("Choix remplacé ou annulé.")
            if choice["payload"]["interest_version"] != self.interest_version(state, place_id):
                blockers.append("Intérêt revu depuis ce choix : choisir à nouveau.")
            if self.now(state).date().isoformat() > choice["payload"]["publication_day"]:
                blockers.append("Créneau passé.")
            summaries.append({"choice_id": choice["id"], "publication_day": choice["payload"]["publication_day"],
                              "active": choice["id"] in active, "points": choice["payload"]["points"],
                              "latest_checks": latest, "remaining_points": pending,
                              "current_blockers": blockers, "scoped_checks_complete": not pending and not blockers,
                              "production_authorized": False})
        return {"place": place, "source_status": {s["id"]: self.source_status(state, s) for s in place["sources"]},
                "choices": summaries, "history": history, "global_verification": "non attestée"}
