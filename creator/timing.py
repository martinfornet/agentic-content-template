"""Caption alignment, typography and beat snapping shared by both renderers."""
import copy
import difflib
import math
import re
import unicodedata

def words(text):
    text = unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode()
    return re.findall(r'[a-z0-9]+', text)


def align(beats, raw, duration, minimum_segment_duration=.5):
    if not math.isfinite(duration) or duration <= 0 or not beats:
        raise ValueError('Durée ou texte vocal invalide.')
    if any(not words(beat['text']) for beat in beats):
        raise ValueError('Segment vocal vide.')
    observed, spans = [], []
    previous_start = -1.0
    for segment in raw['transcription']:
        letters = ''.join(words(segment['text']))
        if not letters:
            continue
        start, end = segment['offsets']['from']/1000, segment['offsets']['to']/1000
        # Whisper can attach several short words to the same estimated instant.
        # Accept those estimates; final beat boundaries must still be usable.
        if not all(math.isfinite(v) for v in (start, end)) or not (0 <= start <= end <= duration + .25) or start < previous_start:
            raise ValueError('Horodatage de transcription invalide.')
        previous_start = start
        for i, letter in enumerate(letters):
            observed.append(letter)
            spans.append((start+(end-start)*i/len(letters),start+(end-start)*(i+1)/len(letters)))
    expected = ''.join(''.join(words(beat['text'])) for beat in beats)
    matcher = difflib.SequenceMatcher(a=expected, b=''.join(observed), autojunk=False)
    mapping = {a+i:b+i for a,b,size in matcher.get_matching_blocks() for i in range(size)}
    coverage = len(mapping)/len(expected)
    tail = len([i for i in range(max(0,len(expected)-15),len(expected)) if i in mapping])/min(15,len(expected))
    # Character comparison tolerates ASR splitting names (lieu indiqué / nom indiqué)
    # without relaxing the completeness threshold. This does not validate pronunciation.
    if coverage < .90 or matcher.ratio() < .90 or tail < .8:
        raise ValueError(f'Voix continue incomplète/divergente : correspondance ASR {coverage:.0%}. Revoir la prise, pas de montage silencieux.')
    boundaries, position = [0.0], 0
    for beat in beats[:-1]:
        position += len(''.join(words(beat['text'])))
        left = max((i for i in mapping if i < position), default=None)
        right = min((i for i in mapping if i >= position), default=None)
        if left is None or right is None or position-left > 15 or right-position > 15:
            raise ValueError('Frontière de sous-titre non alignable.')
        point = (spans[mapping[left]][1]+spans[mapping[right]][0])/2
        boundaries.append(round(point*30)/30)
    boundaries.append(math.ceil(duration*30)/30)
    if any(b-a < minimum_segment_duration for a,b in zip(boundaries,boundaries[1:])):
        raise ValueError('Chronologie vocale invalide.')
    return {'boundaries':boundaries,'coverage':coverage,'match_ratio':matcher.ratio(),
            'method':'local_whisper_word_timing_character_sequence_alignment',
            'pronunciation_human_review_required':True,'transcript':''.join(s['text'] for s in raw['transcription'])}


def subtitle_chunks(beats, max_words=6):
    if not isinstance(max_words, int) or not 3 <= max_words <= 10:
        raise ValueError('Les sous-titres doivent regrouper 3 à 10 mots au maximum.')
    chunks = []
    for beat in beats:
        words = beat['text'].split()
        while words:
            end = min(len(words), max_words)
            if len(words) > max_words:
                strong = [i for i in range(3, end + 1) if re.search(r'[.!?;:…][»”\"]?$', words[i-1])]
                weak = [i for i in range(3, end + 1) if words[i-1].endswith(',')]
                if strong or weak:
                    end = (strong or weak)[-1]
                elif 0 < len(words) - end < 3:
                    end = len(words) - 3
                dangling = {'à', 'a', 'de', 'du', 'des', 'le', 'la', 'les', 'un', 'une', 'et', 'ou', 'pour', 'dans', 'avec', 'derrière', 'se', 'me', 'te'}
                # Keep adjacent capitalized name parts together, without joining
                # the end of one sentence to the start of the next.
                while end > 2 and words[end-1][0].isupper() and words[end][0].isupper() and not re.search(r'[,.!?;:…]$', words[end-1]):
                    end -= 1
                while end > 3 and words[end-1].lower() in dangling:
                    end -= 1
                # French standalone punctuation belongs to the preceding group;
                # this extra token adds no spoken word or timing requirement.
                while end < len(words) and re.fullmatch(r'[:;!?…]+', words[end]):
                    end += 1
            chunks.append({'text': ' '.join(words[:end])})
            words = words[end:]
    return chunks


def snap_music_cuts(shots, pulses, max_shift=.5, min_hold=.65):
    if not shots:return []
    if not isinstance(pulses,list) or any(isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t) or t<0 for t in pulses):
        raise ValueError('Pulsations musicales invalides.')
    result=copy.deepcopy(shots);cuts=[shots[0]['start']]
    for i,shot in enumerate(shots[1:],1):
        target=shot['start']; upper=shots[i]['end']-min_hold
        candidates=[round(t*30)/30 for t in pulses if abs(t-target)<=max_shift and round(t*30)/30>=cuts[-1]+min_hold and round(t*30)/30<=upper]
        cuts.append(min(candidates,key=lambda t:abs(t-target)) if candidates else target)
    cuts.append(shots[-1]['end'])
    if any(b-a<.3-1e-6 for a,b in zip(cuts,cuts[1:])):raise ValueError('Calage musical trop serré.')
    for i,s in enumerate(result):s.update(start=cuts[i],end=cuts[i+1])
    return result


def captions(text, name, output, font, preview=False):
    from PIL import Image, ImageDraw, ImageFont
    canvas = Image.new("RGBA", (1080, 1920))
    draw = ImageDraw.Draw(canvas)
    face = ImageFont.truetype(font, 58)
    words, lines, line = text.split(), [], ""
    for word in words:
        proposal = (line + " " + word).strip()
        if draw.textlength(proposal, font=face) > 850 and line:
            lines.append(line)
            line = word
        else:
            line = proposal
    if line:
        lines.append(line)
    if len(lines) > 5:
        raise ValueError("Sous-titre trop long : raccourcir ou découper ce segment.")
    y = 1380 - len(lines) * 38
    for index, line in enumerate(lines):
        x = (1080 - draw.textlength(line, font=face)) / 2
        draw.text((x, y + index * 76), line, font=face, fill="white", stroke_width=2, stroke_fill="black")
    # Review status belongs in the studio, never in exported pixels.
    canvas.save(output)
