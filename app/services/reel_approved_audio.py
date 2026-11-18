"""Align an approved recording without invoking synthesis, tempo or pause edits."""
import difflib, json, shutil
from app.services.reel_pipeline import digest, file_hash
from app.services.reel_alignment import align, spoken_text, spoken_transcript, words
from app.services.reel_editing import subtitle_chunks, explicit_caption_timeline


def verified_prior_timing(voice, expected_sha, current_raw, beats, duration, aliases):
    # Exact-audio prior timing can repair a new ASR's invalid end timestamp.
    # Never rewrite or truncate the fresh transcript; keep both pieces of evidence.
    directory = voice.parent
    proof_path = directory/'alignment.json'
    timing_path = directory/'source-timing-composition.json'
    audition_path = directory/'audition.json'
    if not all(p.is_file() for p in (proof_path, timing_path, audition_path)):
        raise ValueError('Aucun alignement antérieur lié à la voix exacte.')
    proof = json.loads(proof_path.read_text())
    audition = json.loads(audition_path.read_text())
    if file_hash(voice) != expected_sha or proof.get('audio_sha256') != expected_sha or audition.get('sha256') != expected_sha or not audition.get('alignment_passed'):
        raise ValueError('Preuve d’alignement antérieure non liée à la voix exacte.')
    source = json.loads(timing_path.read_text())
    if source.get('method') != 'source ASR timings translated by exact sample splice offset; full fresh ASR retained separately':
        raise ValueError('Origine des horodatages antérieurs non reconnue.')
    previous_raw = spoken_transcript(source['raw'], aliases)
    a = ''.join(words(' '.join(x.get('text','') for x in previous_raw['transcription'])))
    b = ''.join(words(' '.join(x.get('text','') for x in current_raw['transcription'])))
    if not a or not b or difflib.SequenceMatcher(a=a,b=b,autojunk=False).ratio() < .95:
        raise ValueError('Les transcriptions précédentes et actuelles divergent.')
    alignment = align(beats, previous_raw, duration)
    return previous_raw, alignment, {'method':'prior_source_timing_after_fresh_asr_timestamp_error',
                                     'voice_sha256':expected_sha, 'prior_timing_sha256':file_hash(timing_path),
                                     'prior_alignment_sha256':file_hash(proof_path)}


def prepare_approved(pipeline, inputs, folder, log):
    proof=inputs['approved_narration']
    if proof.get('draft_sha256')!=digest(inputs['draft']) or not proof.get('review_id') or not proof.get('story_id'):
        raise ValueError('La voix validée ne correspond pas au récit de ce montage.')
    voice=pipeline.store.path(proof['audio_path']).resolve()
    if not voice.is_relative_to(pipeline.store.root.resolve()) or not voice.is_file() or file_hash(voice)!=proof['sha256']:
        raise ValueError('Fichier de voix validée absent ou modifié.')
    audio=folder/'narration.wav';shutil.copyfile(voice,audio)
    duration=float(pipeline.probe(audio)['format']['duration'])
    analysis=folder/'alignment.wav';output=folder/'whisper-words'
    pipeline.run_command([pipeline.config['ffmpeg'],'-v','error','-y','-i',str(audio),'-ar','16000','-ac','1',str(analysis)],log)
    pipeline.run_command(['whisper-cli','-m',str(pipeline.config_path('alignment_model')),'-f',str(analysis),'-l','fr','-ojf','-ml','1','-sow','-of',str(output),'-tp','0','-nf','-mc','0','-ng'],log,timeout=600)
    aliases=pipeline.config.get('pronunciation_aliases',{})
    raw=spoken_transcript(json.loads(output.with_suffix('.json').read_text()),aliases)
    beats=inputs['draft']['beats']
    speech_beats=[dict(b,text=spoken_text(b['text'],aliases)) for b in beats]
    timing_evidence={'method':'fresh_whisper_timing'}
    try:
        timing=align(speech_beats,raw,duration)
    except ValueError as error:
        if str(error) != 'Horodatage de transcription invalide.':
            raise
        raw,timing,timing_evidence=verified_prior_timing(voice,proof['sha256'],raw,speech_beats,duration,aliases)
        timing_evidence['fresh_error']=str(error)
        (folder/'reused-source-timing.json').write_text(json.dumps(timing_evidence,ensure_ascii=False,indent=2))
    chunks=subtitle_chunks(beats,pipeline.config.get('subtitle_max_words',6))
    try:
        sub=align([dict(c,text=spoken_text(c['text'],aliases)) for c in chunks],raw,duration,minimum_segment_duration=.1)
        report={'mode':'short_groups'}
    except ValueError as error:
        chunks,sub=beats,timing;report={'mode':'beat_fallback','reason':str(error)}
    report['items']=[dict(start=a,end=b,text=c['text']) for c,a,b in zip(chunks,sub['boundaries'],sub['boundaries'][1:])]
    if inputs.get('caption_timeline_proposal'):
        report={'mode':'explicit_correction', 'items':explicit_caption_timeline(inputs['caption_timeline_proposal'],beats,duration)}
    (folder/'caption-timeline.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (folder/'alignment.json').write_text(json.dumps(dict(timing,audio_sha256=file_hash(audio),approved_narration=proof,synthesis_performed=False,timing_evidence=timing_evidence),ensure_ascii=False,indent=2))
    if file_hash(audio)!=proof['sha256']:raise ValueError('La voix validée a été altérée pendant la préparation.')
    return audio,timing['boundaries']
