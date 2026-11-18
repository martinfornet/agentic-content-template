"""Generic picture rhythm checks; editorial thresholds come from project config."""
import math
from app.services.reel_editing import shot_sequence, visual_assets

VERSION = "content-picture-rhythm-v1"

def requirement(project):
    previous=[r for r in project.get("runs",[]) if r.get("status")=="rendered" and r.get("timeline")]
    configured=int((project.get("visual_policy") or {}).get("minimum_shots", 0))
    floor=max([configured]+[len(r["timeline"]) for r in previous])
    return {"version":VERSION,"minimum_shots":floor,"reference":"configured project policy",
            "rule":"Preserve approved cuts and the configured minimum; source additional distinct assets when required.",
            "reference_run_ids":[r["id"] for r in previous if len(r["timeline"])==floor]}

def audit_plan(project):
    req=requirement(project)
    pool=visual_assets(project)
    plan=project.get("visual_plan") or [{} for _ in project["draft"]["beats"]]
    count=sum(len(shot_sequence(slot,i,len(pool))) for i,slot in enumerate(plan))
    errors=[]
    if count<req["minimum_shots"]:
        errors.append(f"Découpage insuffisant : {count} plans pour {req['minimum_shots']} requis selon la politique configurée.")
    return {**req,"planned_shots":count,"passed":not errors,"errors":errors}

def verify_timeline(project, timeline, *, baseline=None):
    req=requirement(project)
    expected=max(req["minimum_shots"],len((baseline or {}).get("timeline",[])))
    if len(timeline)<expected:
        raise ValueError(f"Rythme dégradé : {len(timeline)} plans livrés pour {expected} requis.")
    durations=[s.get("end",0)-s.get("start",0) for s in timeline]
    if any(not math.isfinite(d) or d<.3 for d in durations):
        raise ValueError("Durée de plan invalide.")
    old_durations=[s["end"]-s["start"] for s in (baseline or {}).get("timeline",[])]
    longest=max([6.0]+old_durations)
    if project.get("studio_review_id") and durations and max(durations)>longest+1/30:
        raise ValueError("Plan fixe trop long : répartir les images sans altérer une voix approuvée.")
    return {**req,"minimum_shots":expected,"actual_shots":len(timeline),"durations":durations,"passed":True,"artistic_validation":False}

def verify_revision(before_project, before_run, after_project, after_run, *, preserve_cuts=True):
    report=verify_timeline(before_project,after_run["timeline"],baseline=before_run)
    old=before_run.get("timeline",[])
    old_voice=before_run.get("approved_narration") or before_run.get("inputs",{}).get("approved_narration") or {}
    new_voice=after_run.get("approved_narration") or after_run.get("inputs",{}).get("approved_narration") or {}
    same_voice=bool(old_voice.get("sha256") and old_voice.get("sha256")==new_voice.get("sha256"))
    if preserve_cuts and same_voice and old:
        new_cuts=[s["end"] for s in after_run["timeline"][:-1]]
        if (abs(old[0]["start"]-after_run["timeline"][0]["start"])>1/30 or
            abs(old[-1]["end"]-after_run["timeline"][-1]["end"])>1/30 or
            any(not any(abs(cut["end"]-new_cut)<=1/30 for new_cut in new_cuts) for cut in old[:-1])):
            raise ValueError("Les coupes du montage existant ont changé malgré une correction des seules images.")
    return report
