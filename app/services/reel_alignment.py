"""Continuous narration cache and local ASR timing; never splice the voice at cuts."""
import difflib
import json
import math
import re
import unicodedata
from pathlib import Path
from app.services.reel_pipeline import digest, file_hash


from creator.timing import words, align


def spoken_text(text, aliases):
    if not isinstance(aliases, dict) or len(aliases)>50:
        raise ValueError('Lexique de prononciation invalide.')
    if any(not isinstance(token,str) or not token.strip() or not isinstance(replacement,str) or not 1<=len(replacement)<=80 for token,replacement in aliases.items()):
        raise ValueError('Entrée de prononciation invalide.')
    for token, replacement in sorted(aliases.items(), key=lambda pair: -len(pair[0])):
        text=re.sub(r'(?<!\w)'+re.escape(token)+r'(?!\w)', lambda _:replacement, text, flags=re.IGNORECASE)
    return text


def spoken_transcript(raw, aliases):
    # Whisper can write a spoken number as digits. Normalize both sides of the
    # comparison without changing its timestamps or relaxing match thresholds.
    return {**raw,'transcription':[{**s,'text':spoken_text(s['text'],aliases)} for s in raw['transcription']]}


def synthesis_texts(draft, speech_beats):
    """Explicit recovery after diagnosis; complete consecutive units of meaning."""
    groups = draft.get('synthesis_groups')
    if groups is None:
        return [' '.join(b['text'] for b in speech_beats)]
    if not draft.get('voice_revision', {}).get('diagnosis'):
        raise ValueError('Diagnostic requis avant synthèse par unités de sens.')
    if (not isinstance(groups, list) or not 2 <= len(groups) <= 6
            or any(not isinstance(g, list) or not g or any(type(i) is not int for i in g) for g in groups)
            or [i for g in groups for i in g] != list(range(len(speech_beats)))):
        raise ValueError('Les unités vocales doivent couvrir chaque passage une seule fois, dans l’ordre.')
    return [' '.join(speech_beats[i]['text'] for i in g) for g in groups]


def prepare(pipeline, inputs, folder, paths, log):
    if inputs.get('approved_narration'):
        from app.services.reel_approved_audio import prepare_approved
        return prepare_approved(pipeline, inputs, folder, log)
    settings = pipeline.config['narration']
    if not (0 <= settings['exaggeration'] <= 1.2 and 0 <= settings['cfg_weight'] <= 1 and .9 <= settings['tempo'] <= 1.2):
        raise ValueError('Réglages de narration hors limites.')
    worker = pipeline.repository/'app/services/reel_narration.py'
    aliases=dict(pipeline.config.get('pronunciation_aliases', {}))
    dictionary=pipeline.repository/'brands/example/pronunciation.json'
    if dictionary.exists():
        aliases.update({r['written']:r['spoken'] for r in json.loads(dictionary.read_text()).get('entries',[])})
    speech_beats=[{**b,'text':spoken_text(b['text'],aliases)} for b in inputs['draft']['beats']]
    text = ' '.join(b['text'] for b in speech_beats)
    units = synthesis_texts(inputs['draft'], speech_beats)
    (folder/'pronunciation-map.json').write_text(json.dumps({'display_text':' '.join(b['text'] for b in inputs['draft']['beats']),
        'spoken_text':text,'aliases':aliases,'human_pronunciation_check_required':True},ensure_ascii=False,indent=2))
    # A pause edit must reuse the raw take, rather than rerun synthesis.
    voice_settings = {k: v for k, v in settings.items() if k != 'pause_edit'}
    identity = {'model':file_hash(Path(paths['voice_checkpoint'])/'t3_mtl23ls_v3.safetensors'),
                'reference':file_hash(paths['voice_reference']), 'worker':file_hash(worker), 'settings': voice_settings,
                'variant':inputs.get('voice_variant',0)}
    if len(units) > 1:
        identity['synthesis_groups'] = inputs['draft']['synthesis_groups']
    key = digest({'identity':identity, 'text':text})
    voice = pipeline.store.path(f'narration-cache/{key}.wav')
    voice.parent.mkdir(parents=True,exist_ok=True)
    receipt = pipeline.store.read(f'narration-cache/{key}.json')
    valid = receipt and receipt.get('identity') == identity and receipt.get('text') == text and voice.exists() and receipt['sha256'] == file_hash(voice)
    parts = [voice] if len(units) == 1 else [voice.with_name(key+f'.unit-{i}.wav') for i in range(len(units))]
    items = []
    if not valid:
        for i, (unit, part) in enumerate(zip(units, parts)):
            part_receipt = pipeline.store.read(str(part.with_suffix('.json').relative_to(pipeline.store.root)))
            if (len(units) > 1 and part.exists() and part_receipt and part_receipt.get('identity') == identity
                    and part_receipt.get('text') == unit and part_receipt.get('sha256') == file_hash(part)):
                continue
            items.append({'text':unit,'spoken_text':unit,'output':str(part),'seed':(int(key[:8],16)+i)%(2**31)})
    manifest = folder/'voice.json'
    manifest.write_text(json.dumps({'checkpoint':paths['voice_checkpoint'], 'reference':paths['voice_reference'],
                                   'identity':identity,'settings':voice_settings,'items':items}))
    if items:
        pipeline.run_command([paths['voice_python'],str(worker),'voice',str(manifest)],log,timeout=3600)
    if not valid and len(units) > 1:
        args = [pipeline.config['ffmpeg'],'-v','error','-y']
        for part in parts:
            args += ['-i',str(part)]
        joined = ''.join(f'[{i}:a]' for i in range(len(parts)))+f'concat=n={len(parts)}:v=0:a=1[a]'
        pipeline.run_command(args+['-filter_complex',joined,'-map','[a]','-c:a','pcm_s16le',str(voice)],log)
        (folder/'synthesis-units.json').write_text(json.dumps({'groups':inputs['draft']['synthesis_groups'],
            'parts':[{'path':str(p),'sha256':file_hash(p)} for p in parts],
            'method':'Complete natural units joined without removing samples or changing speed; full ASR still required.'},indent=2))
    audio = folder/'narration.wav'
    pause_policy = settings.get('pause_edit')
    unedited = folder/'narration-before-pauses.wav' if pause_policy else audio
    pipeline.run_command([pipeline.config['ffmpeg'],'-v','error','-y','-i',str(voice),'-af',f"atempo={settings['tempo']}",'-ar','48000','-ac','2','-c:a','pcm_s16le',str(unedited)],log)
    if pause_policy:
        from app.services.reel_audio import shorten_long_pauses
        pause_report = shorten_long_pauses(unedited, audio, ffmpeg=pipeline.config['ffmpeg'], **pause_policy)
        (folder/'narration-pauses.json').write_text(json.dumps(pause_report, ensure_ascii=False, indent=2))
    duration = float(pipeline.probe(audio)['format']['duration'])
    # Timing belongs to the edited signal and model, not only the raw voice.
    process_key = digest({'audio': file_hash(audio), 'model': file_hash(pipeline.config_path('alignment_model'))})
    alignment_file = pipeline.store.path(f'narration-cache/{key}.{process_key}.alignment.json')
    if not valid or not alignment_file.exists():
        analysis_audio = folder/'alignment.wav'
        pipeline.run_command([pipeline.config['ffmpeg'],'-v','error','-y','-i',str(audio),'-ar','16000','-ac','1',str(analysis_audio)],log)
        output = folder/'whisper-words'
        pipeline.run_command(['whisper-cli','-m',str(pipeline.config_path('alignment_model')),'-f',str(analysis_audio),'-l','fr','-ojf','-ml','1','-sow','-of',str(output),'-tp','0','-nf','-mc','0','-ng'],log,timeout=600)
        raw = json.loads(output.with_suffix('.json').read_text())
        alignment = align(speech_beats,spoken_transcript(raw,aliases),duration)
        alignment.update(audio_sha256=file_hash(audio), model_sha256=file_hash(pipeline.config_path('alignment_model')))
        alignment_file.write_text(json.dumps({'raw':raw,'alignment':alignment},ensure_ascii=False))
    proof = receipt.get('alignment_proofs', {}).get(process_key) if valid else None
    if proof and proof != file_hash(alignment_file):
        raise ValueError('Cache d’alignement modifié : preuve à réexaminer.')
    cached = json.loads(alignment_file.read_text())
    if cached['alignment']['audio_sha256'] != file_hash(audio) or cached['alignment']['model_sha256'] != file_hash(pipeline.config_path('alignment_model')):
        raise ValueError('Preuve d’alignement modifiée : nouvelle analyse requise.')
    alignment = align(speech_beats,spoken_transcript(cached['raw'],aliases),duration)
    from app.services.reel_editing import subtitle_chunks
    chunks = subtitle_chunks(inputs['draft']['beats'], pipeline.config.get('subtitle_max_words', 6))
    try:
        timing = align([{**c,'text':spoken_text(c['text'],aliases)} for c in chunks], spoken_transcript(cached['raw'],aliases), duration, minimum_segment_duration=.1)
        items = [{'start': a, 'end': b, 'text': chunk['text']} for chunk, a, b in zip(chunks, timing['boundaries'], timing['boundaries'][1:])]
        caption_report = {'mode': 'short_groups', 'items': items}
    except ValueError as error:
        # Keep a visible diagnostic rather than inventing word timing.
        items = [{'start': a, 'end': b, 'text': beat['text']} for beat, a, b in zip(inputs['draft']['beats'], alignment['boundaries'], alignment['boundaries'][1:])]
        caption_report = {'mode': 'beat_fallback', 'reason': str(error), 'items': items}
    (folder/'caption-timeline.json').write_text(json.dumps(caption_report, ensure_ascii=False, indent=2))
    (folder/'alignment.json').write_text(json.dumps({**alignment,'audio_sha256':file_hash(audio),'voice_cache_key':key,'asr_transcript_original':''.join(s['text'] for s in cached['raw']['transcription'])},ensure_ascii=False,indent=2))
    proofs = dict(receipt.get('alignment_proofs', {})) if valid else {}
    proofs[process_key] = file_hash(alignment_file)
    pipeline.store.write(f'narration-cache/{key}.json',{'sha256':file_hash(voice),'identity':identity,'text':text,'alignment_proofs':proofs})
    return audio, alignment['boundaries']
