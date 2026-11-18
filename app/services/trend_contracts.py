"""Referential integrity checks for supplied trend evidence, not intelligence."""

from app.models.brand_intelligence import BrandIntelligence
from app.models.source import Source
from app.models.trend import AccountBaseline, ContentObservation, RelevanceLink, TrendSignal


def _registry(values, cls, name):
    registry = {}
    for value in values:
        if not isinstance(value, cls):
            raise ValueError(f"{name} must contain {cls.__name__} records.")
        if value.id in registry:
            raise ValueError(f"Duplicate {name} id: {value.id}")
        registry[value.id] = value
    return registry


def validate_trend_evidence(
    signal: TrendSignal,
    *,
    observations: list[ContentObservation],
    sources: list[Source],
    brand_intelligence: BrandIntelligence,
    baselines: list[AccountBaseline] | None = None,
) -> None:
    """Validate references and context; never certify evidence sufficiency or relevance."""
    signal.__post_init__()
    observation_map = _registry(observations, ContentObservation, "observation")
    source_map = _registry(sources, Source, "source")
    baseline_map = _registry(baselines or [], AccountBaseline, "baseline")

    def source_refs(refs):
        for reference in refs:
            if reference not in source_map:
                raise ValueError(f"Unknown source reference: {reference}")
            if source_map[reference].brand_id != brand_intelligence.brand_id:
                raise ValueError("Evidence Source brand_id must match Brand Intelligence.")

    def domain_links(links: list[RelevanceLink]):
        keys = set()
        for link in links:
            if link.brand_id != brand_intelligence.brand_id:
                raise ValueError("Relevance link brand_id must match Brand Intelligence.")
            try:
                configured = brand_intelligence.domain_relevance_level(link.domain_id)
            except KeyError as exc:
                raise ValueError(f"Unknown relevance domain: {link.domain_id}") from exc
            if configured != link.relevance_level:
                raise ValueError("Relevance link level must match configured domain level.")
            if link.domain_id in keys:
                raise ValueError("Duplicate relevance domain link.")
            keys.add(link.domain_id)

    snapshots = {}
    for observation in observations:
        observation.__post_init__()
        source_refs(observation.source_references)
        domain_links(observation.domain_links)
        for snapshot in observation.performance:
            if snapshot.id in snapshots:
                raise ValueError("Performance snapshot ids must be unique.")
            snapshots[snapshot.id] = (observation.id, snapshot)
            source_refs(snapshot.source_references)

    for baseline in baseline_map.values():
        baseline.__post_init__()
        source_refs(baseline.source_references)
        for reference in baseline.sample_observation_references:
            if reference not in observation_map:
                raise ValueError(f"Unknown baseline sample observation: {reference}")
            sample = observation_map[reference]
            if (sample.platform, sample.creator_id) != (baseline.platform, baseline.creator_id):
                raise ValueError("Baseline samples must match account and platform.")
            if baseline.content_format is not None and sample.content_format != baseline.content_format:
                raise ValueError("Baseline sample format must match its configured format.")

    def baseline_ref(reference, observation):
        if reference is None:
            return
        if reference not in baseline_map:
            raise ValueError(f"Unknown baseline reference: {reference}")
        baseline = baseline_map[reference]
        if (observation.platform, observation.creator_id) != (baseline.platform, baseline.creator_id):
            raise ValueError("Baseline reference must match observation account and platform.")
        if baseline.content_format is not None and observation.content_format != baseline.content_format:
            raise ValueError("Baseline format must match observation format.")

    for observation in observations:
        baseline_ref(observation.baseline_reference, observation)

    supported_content = set()
    supported_platforms = set()
    has_performance_evidence = False
    for support in signal.support:
        if support.observation_id not in observation_map:
            raise ValueError(f"Unknown support observation: {support.observation_id}")
        observation = observation_map[support.observation_id]
        # Exact item identity only; alias/repost detection belongs to future collection.
        key = (observation.platform, observation.content_url)
        if key in supported_content:
            raise ValueError("Repeated observations of the same content cannot duplicate support.")
        supported_content.add(key)
        supported_platforms.add(observation.platform)
        baseline_ref(support.baseline_reference, observation)
        for reference in support.performance_snapshot_references:
            if reference not in snapshots or snapshots[reference][0] != observation.id:
                raise ValueError("Support snapshot must belong to its observation.")
            if snapshots[reference][1].metrics.has_observed_metrics():
                has_performance_evidence = True
    if not has_performance_evidence:
        raise ValueError("Signal support must reference observed performance evidence; unknown is not evidence.")
    if set(signal.platforms) != supported_platforms:
        raise ValueError("Signal platforms must match supporting observations.")
    domain_links(signal.domain_links)
