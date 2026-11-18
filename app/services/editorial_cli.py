"""Parcours local Réserve → À choisir (J+2) → Dossier. Aucun appel externe."""
import argparse
import json
from pathlib import Path

from app.services.editorial_workflow import EditorialWorkflow


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/example/editorial-workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Import idempotent du checkpoint, aucune décision nouvelle.")
    init.add_argument("--seed", default="brands/example/editorial-reserve.json")
    init.add_argument("--owner", default="proprietaire")
    init.add_argument("--repository", default=".")
    sub.add_parser("reserve")
    production = sub.add_parser('production', help='Demande chat dans la même chaîne que le studio ; aucune validation créée.')
    production.add_argument('place')
    production.add_argument('--instruction', required=True)
    production.add_argument('--request-key', required=True, help='Clé stable pour éviter les doublons lors des reprises.')
    chain = sub.add_parser('chain', help='Lire le contexte et les étapes d’un lieu.')
    chain.add_argument('place')
    correction = sub.add_parser('correction', help='Transmettre le retour explicite du propriétaire depuis le chat.')
    correction.add_argument('kind', choices=['story','run'])
    correction.add_argument('target')
    correction.add_argument('--note', required=True)
    correction.add_argument('--audition-sha')
    correction.add_argument('--category', choices=['other','text','pronunciation','rhythm','music','image'], default='other')
    correction.add_argument('--reuse-photo-sha', help='Accord propriétaire explicite pour la photo exacte de fin réutilisée à l’ouverture.')
    correction.add_argument('--reuse-photo-shots', type=int, nargs=2, metavar=('OPENING','ENDING'))
    sub.add_parser("daily", help="Propositions du jour pour J+2, sans choix automatique.")
    dossier = sub.add_parser("dossier")
    dossier.add_argument("place")
    brief = sub.add_parser('writing-brief', help='Dossier de rédaction pour l’assistant, sans appel payant.')
    brief.add_argument('choice', nargs='?')
    brief.add_argument('--place', help='Préparer la rédaction sans créer de choix de publication.')
    story = sub.add_parser('prepare-story', help='Conserver un récit assistant avant choix de publication.')
    story.add_argument('place')
    story.add_argument('--draft', required=True)
    audition = sub.add_parser('audition-story', help='Écouter un récit préparé, sans choix ni validation de publication.')
    audition.add_argument('place')
    proposal = sub.add_parser('propose-reel', help='Enregistrer une proposition assistant sourcée, sans approbation.')
    proposal.add_argument('choice')
    proposal.add_argument('--draft', required=True, help='Proposition JSON rédigée depuis le dossier de rédaction.')
    add = sub.add_parser("add", help="Ajouter un lieu sourcé avec intérêt ouvert.")
    add.add_argument("file", help="JSON ReservePlace ; voir la structure du seed.")
    revise = sub.add_parser("revise", help="Réviser un dossier ; nouvelle confirmation, rejet conservé.")
    revise.add_argument("file", help="JSON ReservePlace avec même id/identity et owner_interest=pending.")
    review = sub.add_parser("interest")
    review.add_argument("place")
    review.add_argument("decision")
    review.add_argument("--reference", required=True)
    choose = sub.add_parser("choose")
    choose.add_argument("place")
    cancel = sub.add_parser("cancel")
    check = sub.add_parser("check")
    check.add_argument("choice")
    check.add_argument("point")
    check.add_argument("result", choices=["verified", "unresolved", "contradicted", "not_applicable"])
    check.add_argument("--source", required=True)
    check.add_argument("--checked-at", required=True, help="Date réelle ISO 8601 avec fuseau.")
    for command in (add, revise, review, choose, cancel, check):
        command.add_argument("--actor", required=True)
        command.add_argument("--role", choices=["owner", "assistant"], required=True)
        if command not in (add, revise):
            command.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    workflow = EditorialWorkflow(args.output)
    try:
        if args.command == "init":
            state = workflow.initialize(json.loads(Path(args.seed).read_text()), args.repository, args.owner)
            result = {"places": len(state["places"]), "store": str(workflow.store.root), "choices_created": 0}
        elif args.command == 'production':
            from app.services.studio_tasks import StudioTasks
            result=StudioTasks(workflow).request_production(args.place,args.instruction,origin='chat',request_key=args.request_key)
        elif args.command == 'correction':
            from app.services.editorial_inbox import EditorialInbox
            from app.services.reel_pipeline import ReelPipeline
            if bool(args.reuse_photo_sha) != bool(args.reuse_photo_shots):
                raise ValueError('Fournir ensemble l’empreinte et les deux positions de l’exception photo.')
            reuse={'asset_sha256':args.reuse_photo_sha,'shots':args.reuse_photo_shots} if args.reuse_photo_sha else None
            result=EditorialInbox(workflow,ReelPipeline(workflow)).submit(args.kind,args.target,'changes',args.note,
                audition_sha=args.audition_sha,correction={'category':args.category},origin='chat',photo_reuse=reuse)
        elif args.command == 'chain':
            from app.services.production_chain import ProductionChain
            result=ProductionChain(workflow).detail(args.place)
        elif args.command == "reserve":
            result = [workflow.dossier(p) for p in workflow.read()["places"]]
        elif args.command == "daily":
            result = workflow.daily()
        elif args.command == "dossier":
            result = workflow.dossier(args.place)
        elif args.command == 'writing-brief':
            from app.services.reel_pipeline import ReelPipeline
            from app.services.reel_writing import writing_brief
            if bool(args.choice) == bool(args.place):
                raise ValueError('Indiquer un choix ou --place, exclusivement.')
            if args.place:
                from app.services.reel_story import place_packet
                result = place_packet(workflow, args.place)
            else:
                result = writing_brief(ReelPipeline(workflow), args.choice)
        elif args.command == 'audition-story':
            from app.services.reel_story_audio import audition
            from app.services.reel_pipeline import ReelPipeline
            result = audition(ReelPipeline(workflow), args.place)
        elif args.command == 'prepare-story':
            from app.services.reel_story import StoryPreparation
            result = StoryPreparation(workflow).prepare(args.place, json.loads(Path(args.draft).read_text()))
        elif args.command == 'propose-reel':
            from app.services.reel_pipeline import ReelPipeline
            result = ReelPipeline(workflow).repropose(args.choice, json.loads(Path(args.draft).read_text()))
        else:
            attribution = {"actor": args.actor, "role": args.role}
            if args.command in {"add", "revise"}:
                result = workflow.add(json.loads(Path(args.file).read_text()), revise=args.command == "revise", **attribution)
            elif args.command == "interest":
                result = workflow.review(args.place, args.decision, args.note, reference=args.reference, **attribution)
            elif args.command == "choose":
                result = workflow.choose(args.place, args.note, **attribution)
            elif args.command == "cancel":
                result = workflow.cancel(args.note, **attribution)
            else:
                result = workflow.check(args.choice, args.point, args.result, args.note, args.source, args.checked_at, **attribution)
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(2, f"Erreur : {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
