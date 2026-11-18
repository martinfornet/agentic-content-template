"""External and internal service abstractions."""

from app.services.acquisition_config import AcquisitionConfig, load_acquisition_config
from app.services.social_acquisition import SocialAcquisitionService
from app.services.observation_analysis import AnalyzedObservation, ObservationAnalysisService, load_analyzed_observation
from app.services.video_analyzer import GeminiVideoAnalyzer, VideoAnalysisConfig, VideoAnalyzer

from app.services.trend_contracts import validate_trend_evidence
from app.services.dataset import DatasetError, load_subject_dataset
from app.services.brand_intelligence import build_brand_intelligence
from app.services.brand_config import BrandConfigError, load_brand_config

from app.services.relevance_config import RelevanceConfigError, load_relevance_config

__all__ = [
    "AnalyzedObservation", "ObservationAnalysisService", "load_analyzed_observation",
    "GeminiVideoAnalyzer", "VideoAnalysisConfig", "VideoAnalyzer",
    "AcquisitionConfig", "load_acquisition_config", "SocialAcquisitionService",
    "validate_trend_evidence",
    "DatasetError", "load_subject_dataset",
    "build_brand_intelligence",
    "BrandConfigError", "load_brand_config",
    "RelevanceConfigError", "load_relevance_config",
]
