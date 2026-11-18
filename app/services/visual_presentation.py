"""Explicit landscape proposals; original composition, no invented pixels."""
import math

LANDSCAPE = 'landscape_fit'
PHOTO = 'photo_fit'
MATTE = '0x182923'


def presentation(asset):
    mode = (asset.get('metadata', {}).get('source') or {}).get('presentation', 'cover')
    if mode not in {'cover', LANDSCAPE, PHOTO} or (mode in {LANDSCAPE, PHOTO} and asset.get('type') != 'IMAGE'):
        raise ValueError('Présentation visuelle inconnue ou réservée aux photos.')
    return mode


def landscape_quality(width, height, config):
    valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (width, height))
    valid = valid and width > height and width / height <= 2 and width >= 1080
    allowed = config.get('visual_sourcing', {}).get('landscape_proposals') is True
    passed = bool(valid and allowed)
    return {'width': width, 'height': height, 'crop_width': width, 'crop_height': height,
            'passed': passed, 'resolution_passed': passed, 'maximum_zoom': 1,
            'upscale_to_output': 1080 / width if valid else None, 'presentation': LANDSCAPE,
            'owner_review_required': True,
            'label': 'Paysage plein large, composition conservée — proposition à examiner',
            'reason': None if passed else 'Photo paysage à revoir : original horizontal de 1080 pixels de large minimum, ratio au plus 2:1, sans agrandissement ; proposition paysage activée requise.'}


def full_photo_quality(width, height, config):
    """Fit a complete square/portrait photo at native output width, no upscale."""
    valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (width, height))
    valid = valid and width >= 1080 and 9 / 16 <= width / height <= 2
    passed = bool(valid and config.get('visual_sourcing', {}).get('full_photo_proposals') is True)
    return {'width': width, 'height': height, 'crop_width': width, 'crop_height': height,
            'passed': passed, 'resolution_passed': passed, 'maximum_zoom': 1,
            'upscale_to_output': 1080 / width if valid else None, 'presentation': PHOTO,
            'owner_review_required': True,
            'label': 'Photo entière, largeur native — proposition à examiner',
            'reason': None if passed else 'Photo entière : largeur native ≥1080, ratio de 9:16 à 2:1 et propositions activées requis ; aucun agrandissement.'}


def asset_filter(asset, seconds, motion=None):
    from app.services.reel_editing import photo_filter
    if presentation(asset) in {LANDSCAPE, PHOTO}:
        return ('scale=1080:-2:out_range=tv:out_color_matrix=bt709,setsar=1,'
                f'pad=1080:1920:0:(oh-ih)/2:color={MATTE},fps=30')
    # Reset a seek between source frames before FPS conversion. Every excerpt
    # starts at zero, with no missing first frame or accumulated concat drift.
    prefix = 'setpts=PTS-STARTPTS,' if asset['type'] == 'VIDEO' else ''
    return prefix + photo_filter(seconds, motion if asset['type'] == 'IMAGE' else None)


def sample_window(asset):
    """Compare actual picture pixels, not predominantly the surrounding matte."""
    if presentation(asset) not in {LANDSCAPE, PHOTO}:
        return 'crop=1080:900:0:0'
    stream = next(s for s in asset['metadata']['probe']['streams'] if s.get('codec_type') == 'video')
    height = round(1080 * stream['height'] / stream['width'] / 2) * 2
    top = (1920 - height) // 2
    return f'crop=1080:{min(height, 1180-top)}:0:{top}'
