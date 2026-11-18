"""Generic source-guided copy scaffold; brand direction belongs in configuration."""
import re

def propose(subject):
    name = str(subject.get("name") or subject.get("title") or "Sujet").strip()
    reason = str(subject.get("reason") or "").strip()
    source_ids = list(subject.get("reason_source_ids") or subject.get("source_ids") or [])
    beats = [(f"{name}. Quel détail documenté mérite d’être retenu ?", ["subject_identity"])]
    if reason:
        for sentence in (part.strip() for part in re.split(r"[.;]", reason) if part.strip()):
            beats.append((sentence, ["subject_fact"]))
    beats.append(("Les informations pratiques et les affirmations à confirmer restent à vérifier auprès de leurs sources.", ["current_details"]))
    lines = [{"text": text, "points": points, "source_ids": source_ids if points != ["current_details"] else []}
             for text, points in beats]
    return {"angle": f"{name} : un angle à préciser à partir des éléments sourcés.",
            "caption": f"{name}\n{reason}".strip(), "beats": lines,
            "used_points": sorted({point for beat in lines for point in beat["points"]}),
            "method": "source_guided_composition_v1",
            "notice": "Brouillon à vérifier et à faire approuver par une personne."}
