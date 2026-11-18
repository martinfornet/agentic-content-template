"""One correction scope for the producer, critic and delivery guards."""
import re

VISUAL = re.compile(r'image|photo|vid[ée]o|fa[çc]ade|premi[eè]re?\s+(?:image|plan)|montage|plan\b|visuel', re.I)
PACE = re.compile(r'rythme|dynam|cadence|coupe|dur[ée]e|lent|rapide|long|encha[îi]ne', re.I)
VOICE = re.compile(r'voix|voca[lu]|pronon|diction|intonation|parl|respir|silence', re.I)
FACADE = re.compile(r'fa[çc]ade|image\s+de\s+la\s+face\b', re.I)


def scope_for(task):
    r = task['review']; note = r.get('note', '')
    category = (r.get('correction') or {}).get('category', 'other')
    requests=task.get('correction_requests',[])
    if requests: note='\n'.join(dict.fromkeys([q['note'] for q in requests]+[note]))
    categories={category,*[q['category'] for q in requests]}
    run = r['kind'] == 'run'
    visual = 'image' in categories or bool(VISUAL.search(note))
    visual_pace = bool(PACE.search(note)) and (visual or run and not VOICE.search(note))
    music_dynamics = bool(re.search(r'(?:musique|piste musicale).{0,100}(?:baiss|constant|piste|voix|parle)|(?:deux|2)\s+pistes?.{0,80}(?:musique|voix|superpos|parall)', note, re.I))
    voice_edit = bool(categories & {'pronunciation', 'text'}) or r['decision'] == 'edited'
    voice_edit |= 'rhythm' in categories and not visual_pace
    actionable = re.sub(r'(?:ne\s+)?(?:change\w*|modifi\w*|refai\w*|repren\w*|enl[èe]ve\w*|supprim\w*|retir\w*)\s+(?:surtout\s+)?(?:pas|jamais)[^.!?;]*', '', note, flags=re.I)
    voice_edit |= bool(re.search(r'(?:corrig\w*|refai\w*|chang\w*|repren\w*|retravaill\w*|modifi\w*|amélior\w*)\s+(?:(?:la|le|les|ma|mon|cette|ce|une|un)\s+)?(?:voix|vocal|pronon|diction)', actionable, re.I))
    # Explicit requests about the spoken text or its delivery apply even when
    # the same review also asks for image changes on a rendered Reel.
    voice_edit |= bool(re.search(r'(?:rythme|d[ée]bit|vitesse|cadence)\s+(?:de\s+)?(?:la\s+)?(?:parole|voix|narration)|(?:voix|parole|narration).{0,45}(?:trop\s+vite|acc[ée]l[ée]r|ralent|plus\s+lent)', actionable, re.I))
    voice_edit |= bool(re.search(r'vocabulaire|(?:texte|mots?).{0,35}(?:adapt|chang|modifi|r[ée][ée]cri)|(?:adapt|chang|modifi|r[ée][ée]cri).{0,35}(?:texte|vocabulaire)', actionable, re.I))
    # A requested reformulation of a story necessarily changes its spoken words.
    if not run and not re.search(r'(?:ne\s+pas|sans|ne\s+jamais)\s+reformul', note, re.I):
        voice_edit |= bool(re.search(r'\breformul(?:er|e[rz]?|ons|ation)\b', actionable, re.I))
    voice_edit |= bool(re.search(r'(?:enl[èe]v\w*|supprim\w*|retir\w*)\s+(?:la\s+|le\s+|cette\s+|ce\s+)?(?:partie|passage|mention)\b', actionable, re.I))
    voice_edit |= bool(re.search(r'(?:point d.exclamation|point d.interrogation|ponctuation).{0,100}(?:flat|plat|express|inton|cons[ée]quence|prononc)|(?:expressivit[ée]|intonation|diction).{0,80}(?:flat|plat|ajout|am[ée]lior|plus|manque)', actionable, re.I))
    # Delivery direction for spoken phrases applies to the voice even when
    # the request does not explicitly use the word « voix ».
    delivery = re.sub(r'\b(?:ne\s+pas|sans|jamais)\s+(?:dire|prononcer|lire|r[ée]citer)\b[^.!?;]*', '', actionable, flags=re.I)
    voice_edit |= bool(re.search(r'\b(?:dit(?:e|s|es)?|dire|prononc\w*|lire|lu(?:e)?|r[ée]cit\w*)\b[^.!?;]{0,100}\b(?:enjou[ée]\w*|joueur\w*|souriant\w*|expressif\w*|expressive\w*|enthousiaste\w*)\b', delivery, re.I))
    if not run and not visual and category not in {'music', 'image'}:
        voice_edit |= bool((not music_dynamics and (VOICE.search(note) or PACE.search(note))) or re.search(r'hook|accroche|closing|conclusion|vocabulaire|r[ée]p[ée]t(?:er|ition|e)|mots?\b', actionable, re.I))
    music_edit = music_dynamics or 'music' in categories or bool(re.search(r'(?:chang|remplac|modifi|baiss|mont|augment|hausse|volume).{0,35}(?:musique|mix)|(?:musique|mix).{0,25}(?:fort|faible)', note, re.I))
    accepted = r['decision'] == 'keep'
    return {'version': 1, 'request_id': r.get('id', task.get('id')), 'original_request': note,
            'visuals': visual, 'visual_timing': visual_pace,
            'facade_opening': bool(FACADE.search(note) and re.search(r'premi|d[ée]but|ouvert|hook|accroche', note, re.I)),
            'preserve_voice': accepted or not voice_edit,
            'preserve_music': accepted or not music_edit,
            'preserve_cuts': run and not visual_pace and not voice_edit,
            'caption_readability_repairs': run,
            'quality_rules_unchanged': True,
            'rules': ['La demande réelle prime sur les préférences du critique.',
                      'Préserver le nombre de plans demandé, la résolution native, les sources et leur diversité, et l’usage unique.',
                      'Le dynamisme ne rend obligatoires ni vidéo, ni zoom, ni plans supplémentaires. Justifier les durées et l’alternance par le récit.',
                      'Les retouches de lisibilité des sous-titres conservent les mots et la voix validés.',
                      'Aucun mouvement sur photo_fit/landscape_fit ; les autres mouvements restent soumis à la charte et à la définition native.']}


def protect_audio(scope, previous, current):
    if scope['preserve_voice'] and previous.get('sha256') != current.get('sha256'):
        raise ValueError('Cette correction doit conserver la voix validée.')
    if scope['preserve_music'] and previous.get('music', {}).get('sha256') != current.get('music', {}).get('sha256'):
        raise ValueError('Cette correction doit conserver la musique validée.')
