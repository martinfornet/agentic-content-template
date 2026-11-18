"""Choose only the proposal bound to the actual current owner review."""
KEYS=('revision_mix_proposal','revision_voice_proposal','revision_story_proposal')

def active_revision(project, review):
    review_id=review.get('id') if isinstance(review,dict) else None
    current=[key for key in KEYS if isinstance(project.get(key),dict) and project[key].get('review_id')==review_id and review_id]
    if len(current)>1:
        raise ValueError('Une seule proposition active par correction propriétaire.')
    return current[0] if current else None
