"""Readable editorial memory derived from actual feedback, never model training."""
from pathlib import Path
import json,re
import hashlib
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

STYLE=re.compile(r'story\s*telling|narrati|intonation|dict[ée]e|vocabulaire|rythme|prononci|accroche|hook|closing|conclusion|granularit|fluid|trop\s+(?:AI|IA)|silence|blanc|phrase|chute|façon d.aborder|façon de raconter|ton\b|voix|atmosph[èe]re|exemples? concrets?|r[ée]p[ée]tition|r[ée]p[ée]ter|g[ée]n[ée]rique|orthographe|structure|jeune|trendy',re.I)


def general_guidance(note):
 from app.services.revision_contract import VISUAL, PACE, VOICE, FACADE
 rules=[]
 visual=bool(VISUAL.search(note));visual_pace=visual and bool(PACE.search(note))
 if visual_pace:rules.append('Travailler le rythme visuel : durées et alternance des images selon le récit ; préserver la voix validée sauf demande explicite sur celle-ci.')
 if FACADE.search(note) and re.search(r'premi|début|ouvert|hook|accroche',note,re.I) and re.search(r'toujours|always|chaque|tous les',note,re.I):
  rules.append('Ouvrir chaque Reel par une photo pertinente de la façade du lieu, reconnaissable et engageante ; respecter la qualité et les sources.')
 if visual and re.search(r'atmosph|cohéren|ambiance',note,re.I):rules.append('Faire ressentir l’atmosphère du lieu par une sélection visuelle cohérente, pertinente et documentée.')
 if re.search(r'couverture',note,re.I):rules.append('Préserver la cohérence des couvertures : même gabarit, taille, graisse et traitement visuel ; conserver les précisions du retour original.')
 if re.search(r'prononc|prononce|dict[ée]e',note,re.I):rules.append('Vérifier la prononciation des noms et termes propres à chaque lieu.')
 if re.search(r'ponctuation|rush|rythme|pos[ée]?[rs]?|respir|silence|blanc|fluid',note,re.I) and (not visual_pace or VOICE.search(note)):rules.append('Relier les idées d’une même phrase ; réserver les pauses aux respirations et changements de sens, sans raccourcissement systématique.')
 if re.search(r'story\s*telling|narrati|anecdote|d[ée]tail|fond|surface|atmosph[èe]re|cocktail|plat',note,re.I):rules.append('Développer un récit concret sur le lieu et son atmosphère avec des détails et exemples documentés ; éviter le remplissage et les conseils génériques.')
 if re.search(r'ne (?:plus|pas).*?(?:source|article|site)|(?:source|article|site).*?ne (?:plus|pas)',note,re.I):rules.append('Ne pas raconter la recherche ni citer les supports dans la narration ; conserver les sources et les attributions nécessaires dans le dossier éditorial.')
 if re.search(r'r[ée]p[ée]tition|r[ée]p[ée]ter|r[ée]p[èe]te',note,re.I):rules.append('Relire les phrases voisines pour éviter les répétitions rapprochées ; varier naturellement sans sacrifier la précision.')
 if re.search(r'vocabulaire|jeune|trendy|boring',note,re.I):rules.append('Employer un vocabulaire vivant et actuel, adapté à une audience jeune et au lieu ; éviter les tournures administratives et le jargon forcé.')
 if re.search(r'invent|v[ée]rifi|source|avis|internet',note,re.I):rules.append('Étayer les anecdotes et détails par les sources et attribuer les appréciations ; ne pas inventer une expérience.')
 if re.search(r'r[ée]p[ée]tition|g[ée]n[ée]rique|accroche|hook|phrase|chute|closing|conclusion',note,re.I) and (not visual or re.search(r'texte|phrase|conclusion|closing|narrati',note,re.I)):rules.append('Ancrer l’accroche dans un détail concret et conclure sur l’idée propre au lieu ; éviter les fins interchangeables.')
 if re.search(r'voix|parole|prononc|r[ée]alisme|r[ée]aliste|naturel|trop\s+(?:AI|IA)|fluid',note,re.I):rules.append('Comparer la diction à la référence approuvée : timbre, appuis, transitions et naturel ; une QA technique ne vaut pas acceptation perceptive.')
 if re.search(r'granularit',note,re.I):rules.append('Travailler le relief des unités de sens avec la recette vocale active ; préserver les passages acceptés, sans découpage mot à mot ni accélération.')
 return ' '.join(rules)

def active_rules(workflow):
 state=workflow.read();brand=Path(state['repository'])/'brands'/state['brand_id']/'editorial-memory-policy.json'
 candidates={}
 for path in [brand]+[workflow.store.root/'editorial-memory'/n for n in ('policy.json','remote-policy.json')]:
  if path.exists():
   for rule in json.loads(path.read_text()).get('rules',[]):candidates.setdefault(rule['id'],rule)
 replaced={ident for rule in candidates.values() if rule.get('status','active')=='active' for ident in rule.get('supersedes',[])}
 return [dict(r,status='active') for ident,r in candidates.items() if r.get('status','active')=='active' and ident not in replaced]

def feedback_interpretation(note, scope='auto'):
 from app.services.revision_contract import VISUAL, PACE, VOICE, FACADE
 visual=bool(VISUAL.search(note))
 explicit_general=bool(re.search(r'toujours|always|chaque|tous les',note,re.I))
 resolved=scope if scope in {'general','place'} else 'general' if STYLE.search(note) or explicit_general or visual and PACE.search(note) else 'place'
 domains=[]
 if visual:domains.append('images')
 if visual and PACE.search(note):domains.append('visual_timing')
 if VOICE.search(note):domains.append('voice')
 if re.search(r'couverture',note,re.I):domains.append('cover')
 if re.search(r'texte|narrati|storytelling|phrase|conclusion|accroche|hook|vocabulaire|r[ée]p[ée]t|source|article',note,re.I):domains.append('text')
 return {'scope':resolved,'scope_requested':scope,'scope_inferred':scope=='auto',
         'domains':domains,
         'instruction':(general_guidance(note) or 'Respecter la préférence de forme ; relire le retour original dans le contexte du lieu avant application.') if scope=='auto' and resolved=='general' else note,
         'original_note':note}

def update_feedback_memory(workflow,review_id,scope,active=True):
 if scope not in {'auto','general','place'} or not isinstance(active,bool):raise ValueError('Portée de mémoire invalide.')
 if not re.fullmatch(r'[a-f0-9]{32}',review_id):raise ValueError('Retour invalide.')
 review=workflow.store.read('reels/inbox/'+review_id+'.json')
 if not review:raise ValueError('Retour introuvable.')
 import fcntl
 from datetime import datetime,UTC
 root=workflow.store.root/'editorial-memory';root.mkdir(exist_ok=True)
 with (root/'memory.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  data=workflow.store.read('editorial-memory/overrides.json',{})
  previous=data.get(review_id,{})
  if previous.get('scope')==scope and previous.get('active')==active:return previous
  value={'scope':scope,'active':active,'changed_at':datetime.now(UTC).isoformat(),'actor':workflow.read()['owner_id']}
  value['history']=previous.get('history',[])+[{k:v for k,v in previous.items() if k!='history'}] if previous else []
  data[review_id]=value;workflow.store.write('editorial-memory/overrides.json',data)
 return value

def memory_view(workflow,place_id):
 entries=[]
 entries=[dict(r,scope='general',source='owner_project_instruction') for r in active_rules(workflow)]
 overrides=workflow.store.read('editorial-memory/overrides.json',{})
 reviews=sorted((json.loads(p.read_text()) for p in (workflow.store.root/'reels/inbox').glob('*.json')),key=lambda r:(r['created_at'],r['id']))
 for r in reviews:
  override=overrides.get(r['id'],{})
  if not override.get('active',True) or not r.get('note','').strip():continue
  scope=override.get('scope',r.get('memory_scope','auto'))
  interpretation=feedback_interpretation(r['note'],scope);resolved=interpretation['scope']
  if resolved=='place' and r['place_id']!=place_id:continue
  instruction=interpretation['instruction']
  e={'id':r['id'],'scope':resolved,'scope_requested':scope,'scope_inferred':scope=='auto','domains':interpretation['domains'],'place_id':r['place_id'],'instruction':instruction,'decision':r['decision'],'source':'owner_feedback','source_review_id':r['id'],'created_at':r['created_at'],'application':'Style et forme uniquement pour les autres lieux ; aucun fait propre au lieu à transférer.'}
  if r.get('new_story_id') and r['kind']=='story':
   root=workflow.store.root/'reels/story-proposals'
   before=root/(r['target_id']+'.json');after=root/(r['new_story_id']+'.json')
   if before.exists() and after.exists():
    a=json.loads(before.read_text())['draft'];b=json.loads(after.read_text())['draft']
    e['text_changes']=[{'before':x['text'],'after':y['text']} for x,y in zip(a['beats'],b['beats']) if x['text']!=y['text']]
    if a['caption']!=b['caption']:e['caption_change']={'before':a['caption'],'after':b['caption']}
  entries.append(e)
  if scope=='auto' and resolved=='general' and r['place_id']==place_id:
   entries.append({'id':r['id']+'-local-context','scope':'place','place_id':place_id,'instruction':r['note'],'source':'owner_feedback_original_context','source_review_id':r['id'],'created_at':r['created_at']})
 return {'entries':entries,'fingerprint':fingerprint(entries),'charter_version':fingerprint(active_rules(workflow)),'model_weights_updated':False,'instructions':['Appliquer les consignes éditoriales générales aux prochains textes, sans transférer de faits ou sensations d’un autre lieu.','Conserver les exceptions propres au lieu et les corrections avant/après dans leur contexte.','Les retours sont des préférences de rédaction, pas des preuves factuelles.','Un retour enregistré ne réécrit pas automatiquement les anciens récits.']}


def production_memory_version(workflow,place_id):
 # Other places' new feedback guides the NEXT job, without cancelling this one.
 view=memory_view(workflow,place_id)
 return fingerprint({'charter':view['charter_version'],'local':[e for e in view['entries'] if e.get('place_id')==place_id]})
