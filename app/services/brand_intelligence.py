"""Build derived Brand Intelligence without IO, inference, or persistence."""

from app.models.brand import Brand
from app.models.brand_intelligence import BrandIntelligence
from app.models.relevance import RelevanceUniverse


def build_brand_intelligence(
    brand: Brand, relevance_universe: RelevanceUniverse
) -> BrandIntelligence:
    """Combine loaded configuration into a validated, isolated decision context."""
    return BrandIntelligence(brand, relevance_universe)
