"""Deterministic, bounded edits to a PCM WAV, always producing a new file and receipt."""
import json,math,wave
from pathlib import Path
from app.services.studio_delivery import sha256

def shorten_pause(source,destination,start,end,*,max_rms=0.015):
    """Only remove an explicitly located quiet interval; never infer silence from text."""
    import array
    source,destination=Path(source),Path(destination)
    if source.resolve()==destination.resolve() or destination.exists():raise ValueError('A new output file is required.')
    if any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in (start,end)) or not 0<=start<end or end-start>3:raise ValueError('Pause interval must be 0–3 seconds.')
    with wave.open(str(source),'rb') as audio:
        params=audio.getparams();data=audio.readframes(audio.getnframes())
    if params.sampwidth!=2:raise ValueError('16-bit PCM input required.')
    if end>params.nframes/params.framerate:raise ValueError('Interval outside audio.')
    frame=params.sampwidth*params.nchannels;a=int(start*params.framerate)*frame;b=int(end*params.framerate)*frame
    values=array.array('h',data[a:b]);rms=(sum(x*x for x in values)/max(1,len(values)))**.5/32768
    if rms>max_rms:raise ValueError('This interval contains sound; explicit listening and a narrower interval are required.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(destination),'wb') as audio:audio.setparams(params);audio.writeframes(data[:a]+data[b:])
    receipt={'operation':'shorten_located_pause','source_sha256':sha256(source),'sha256':sha256(destination),'start':start,'end':end,'removed_seconds':(b-a)/frame/params.framerate,'rms':rms,'unaffected_pcm_preserved':True,'listening_approved':False}
    destination.with_suffix('.edit.json').write_text(json.dumps(receipt,indent=2));return receipt


def extend_located_pauses(source, destination, pauses):
    """Extend explicitly diagnosed quiet gaps; retain every original PCM sample.

    Locations are source-relative seconds, never inferred from punctuation or
    word timestamps alone. This is a bounded repair, not a global pacing rule.
    """
    import array
    import sys
    source, destination = Path(source), Path(destination)
    receipt_path = destination.with_suffix('.edit.json')
    if source.resolve() == destination.resolve() or destination.exists() or receipt_path.exists():
        raise ValueError('New output and receipt files are required.')
    if not isinstance(pauses, list) or not 1 <= len(pauses) <= 4:
        raise ValueError('One to four diagnosed pauses are required.')
    with wave.open(str(source), 'rb') as audio:
        params = audio.getparams()
        data = audio.readframes(params.nframes)
    if params.sampwidth != 2 or params.comptype != 'NONE':
        raise ValueError('16-bit PCM input required.')
    rate, width = params.framerate, params.sampwidth * params.nchannels
    values = array.array('h', data)
    if sys.byteorder != 'little':
        values.byteswap()
    edits, total, previous_end = [], 0, 0
    for pause in pauses:
        start, end, extra = (pause.get(k) for k in ('start', 'end', 'added_seconds'))
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end, extra)):
            raise ValueError('Finite pause positions and duration are required.')
        if not 0 <= previous_end <= start < end <= params.nframes / rate or not .02 <= end-start <= .5 or not .04 <= extra <= .6:
            raise ValueError('Ordered quiet windows of 20–500 ms and additions of 40–600 ms are required.')
        if not isinstance(pause.get('reason'), str) or not pause['reason'].strip():
            raise ValueError('A phrase-specific pause diagnosis is required.')
        a, b = round(start*rate), round(end*rate)
        window = values[a*params.nchannels:b*params.nchannels]
        rms = (sum(x*x for x in window) / len(window))**.5 / 32768
        if rms > .006 or max(map(abs, window)) / 32768 > .04:
            raise ValueError('The located interval contains sound; do not split speech.')
        # Join near zero in the interior. No fades, time stretching or gain change
        # are applied to original speech, including its subsequent body.
        margin = max(1, round(.005*rate))
        point = min(range(a+margin, b-margin), key=lambda n: (max(abs(x) for x in values[n*params.nchannels:(n+1)*params.nchannels]), abs(n-(a+b)//2)))
        if max(abs(x) for x in values[point*params.nchannels:(point+1)*params.nchannels]) > 16:
            raise ValueError('No sufficiently quiet join in the selected interval.')
        added = round(extra*rate)
        total += added
        previous_end = end
        edits.append(dict(pause, source_frame=point, added_frames=added,
                          added_seconds=added/rate, rms=rms))
    if total/rate > 1.2:
        raise ValueError('The localized repair exceeds 1.2 seconds; review the interpretation.')
    cursor, parts = 0, []
    for edit in edits:
        point = edit['source_frame']*width
        parts.extend((data[cursor:point], b'\0'*(edit['added_frames']*width)))
        cursor = point
    parts.append(data[cursor:])
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects against an output appearing during work.
    with destination.open('xb') as handle:
        with wave.open(handle, 'wb') as audio:
            audio.setparams(params)
            audio.writeframes(b''.join(parts))
    receipt = dict(operation='extend_located_pauses', source_sha256=sha256(source),
                   sha256=sha256(destination), edits=edits, added_seconds=total/rate,
                   original_pcm_preserved=True, speed_changed=False,
                   naturalness_assessed=False, listening_approved=False)
    with receipt_path.open('x') as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
    return receipt
