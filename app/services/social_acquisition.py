"""Bounded autonomous discovery and evidence acquisition; no S02/T02 behavior."""
from __future__ import annotations
from datetime import datetime, UTC, timedelta
import time

from app.models.acquisition import (
    AcquiredContent, AcquisitionAttempt, AcquisitionFailure, AcquisitionError, AccountSnapshot,
    AttemptStatus, Decision, DiscoveryRequest, MediaReference, MediaState, WatchlistEntry, WatchStatus, now, new_id,
)
from app.models.source import Source, SourceType
from app.models.trend import ContentObservation, PerformanceSnapshot, RawMetrics
from app.models.trend import RelevanceLink
from app.services.acquisition_store import stable_key
from app.services.discovery import assess_candidate


class SocialAcquisitionService:
    def __init__(self, context, config, provider, evaluator, store, downloader, *, sleep=time.sleep):
        if context.brand_id != config.brand_id:
            raise ValueError("Acquisition brand must match Brand Intelligence.")
        self.context, self.config, self.provider, self.evaluator = context, config, provider, evaluator
        self.store, self.downloader, self.sleep = store, downloader, sleep
        manifest = store.read("manifest.json")
        if manifest and manifest.get("brand_id") != context.brand_id:
            raise ValueError("Acquisition storage root belongs to a different brand.")
        store.write("manifest.json", {"schema_version": 1, "brand_id": context.brand_id}, immutable=True)
        self.run_id = new_id()
        self.summary = {"run_id": self.run_id, "started_at": now(), "provider": provider.name,
                        "attempts": [], "decisions": [], "observations": [], "errors": [],
                        "media_attempts": 0, "media_successes": 0, "provider_cost_usd": None}
        self.summary["media_download_requests"] = 0

    def _call(self, operation, target, function, **kwargs):
        pending = next((p for p in self.store.unfinished_attempts()
                        if p["operation"] == operation and p["target_reference"] == target), None)
        if pending:
            error = AcquisitionError(**pending["error"]) if pending.get("error") else None
            attempt = AcquisitionAttempt(**{**pending, "status": AttemptStatus.RUNNING, "error": error})
            if not attempt.provider_job_id:
                raise AcquisitionFailure(AcquisitionError("ambiguous_job_start", "Unfinished request has no job id; inspect provider before retrying."))
            # Resume with the original request, not today's changed history window.
            kwargs = dict(attempt.request_parameters)
            if isinstance(kwargs.get("request"), dict):
                kwargs["request"] = DiscoveryRequest(**kwargs["request"])
        else:
            attempt = AcquisitionAttempt(self.run_id, operation, target, self.provider.name, AttemptStatus.RUNNING)
            attempt.request_parameters = {name: value.to_dict() if hasattr(value, "to_dict") else value for name, value in kwargs.items()}
        self.store.save_attempt(attempt)
        self.summary["attempts"].append(attempt.id)

        def on_job(job_id):
            attempt.provider_job_id = job_id
            self.store.save_attempt(attempt)

        try:
            result = function(job_id=attempt.provider_job_id, on_job=on_job, **kwargs)
            source = Source(brand_id=self.context.brand_id, type=SourceType.FILE,
                            title=f"{operation} public social response", url_or_path=f"sources/{attempt.id}-raw.json",
                            id=attempt.id + "-source", metadata={"provider": self.provider.name, "job_id": result.job_id,
                            "retrieved_at": result.retrieved_at, "measured_at": result.measured_at,
                            "limitations": result.limitations, "cost_usd": result.cost_usd,
                            "request_target": target, "request_parameters": attempt.request_parameters})
            self.store.write(source.url_or_path, result.raw, immutable=True)
            self.store.save_source(source)
            attempt.status = AttemptStatus.SUCCEEDED if result.items else AttemptStatus.PARTIAL
            failures = [item for item in result.items if item.get("kind") == "failure"]
            if failures:
                attempt.status = AttemptStatus.PARTIAL if len(failures) < len(result.items) else AttemptStatus.FAILED
                self.summary["errors"].extend({"attempt_id": attempt.id, "target": item.get("url"), **item["error"]}
                                              for item in failures)
            attempt.source_references, attempt.ended_at = [source.id], now()
            attempt.error = (AcquisitionError("content_unavailable", "Some provider targets were unavailable; inspect raw evidence.")
                             if failures else None)
            if result.cost_usd is not None:
                self.summary["provider_cost_usd"] = (self.summary["provider_cost_usd"] or 0) + result.cost_usd
            self.store.save_attempt(attempt)
            return result, source, attempt
        except AcquisitionFailure as exc:
            attempt.error = exc.error
            if exc.error.provider_cost_usd is not None:
                self.summary["provider_cost_usd"] = (self.summary["provider_cost_usd"] or 0) + exc.error.provider_cost_usd
            attempt.provider_job_id = exc.error.provider_job_id or attempt.provider_job_id
            ambiguous_start = exc.error.code == "network_failure" and not attempt.provider_job_id
            attempt.status = (AttemptStatus.PENDING if ambiguous_start or (exc.error.retryable and attempt.provider_job_id)
                              else AttemptStatus.FAILED)
            attempt.ended_at = now()
            self.store.save_attempt(attempt)
            self.summary["errors"].append({"attempt_id": attempt.id, **exc.error.to_dict()})
            if exc.error.code in {"authentication_failure", "payment_required"}:
                raise
            return None

    def _assess(self, item, domains, source, reason):
        # No metrics, media analysis or provider-ranking fields go to eligibility.
        metadata = {name: item.get(name) for name in ("kind", "name", "bio", "caption", "url", "handle")}
        metadata["recent_captions"] = [p.get("caption") for p in item.get("raw", {}).get("latestPosts", [])[:3]]
        if hasattr(self.evaluator, "last_response"):
            self.evaluator.last_response = None
        try:
            decision = assess_candidate(self.context, self.evaluator, candidate=item["url"], metadata=metadata,
                                        proposed_domains=domains, sources=[source.id], reason=reason)
        except (AcquisitionFailure, ValueError) as exc:
            if isinstance(exc, AcquisitionFailure) and exc.error.code in {"authentication_failure", "payment_required"}:
                raise
            from app.models.acquisition import RelevanceDecision
            decision = RelevanceDecision(item["url"], Decision.UNCERTAIN, [], "Relevance assessment unavailable.",
                                         [source.id], reason, "assessment_failure", proposed_domain_links=[
                                             RelevanceLink(self.context.brand_id, domain,
                                                 self.context.domain_relevance_level(domain), "Proposed discovery context")
                                             for domain in domains])
            self.summary["errors"].append({"operation": "source_eligibility", "candidate": item["url"],
                                         "code": exc.error.code if isinstance(exc, AcquisitionFailure) else "invalid_assessment"})
        response = getattr(self.evaluator, "last_response", None)
        if response is not None:
            evidence = Source(self.context.brand_id, SourceType.FILE, "Source eligibility assessment",
                              f"decisions/{decision.id}-response.json", metadata={"model": decision.method,
                              "assessed_at": decision.assessed_at, "input_source_references": [source.id]})
            self.store.write(evidence.url_or_path, response, immutable=True)
            self.store.save_source(evidence)
            decision.source_references.append(evidence.id)
        self.store.write(f"decisions/{decision.id}.json", decision.to_dict(), immutable=True)
        self.summary["decisions"].append(decision.id)
        return decision

    def _entry(self, item, request_id, domains):
        handle = item.get("handle")
        account_id = item.get("account_id") if item["kind"] == "content" else item.get("id")
        if not handle:
            return None
        entries = self.store.load_watchlist()
        key = self.store.find_account_key(account_id, handle)
        if key not in entries:
            entry = WatchlistEntry(key, f"https://www.instagram.com/{handle}/", platform_account_id=account_id, handle=handle)
            entries[key] = entry.to_dict()
        entry = entries[key]
        entry["handle"], entry["platform_account_id"] = handle, account_id or entry.get("platform_account_id")
        entry.setdefault("proposed_domain_ids", [])
        entry["proposed_domain_ids"] = sorted(set(entry["proposed_domain_ids"] + domains))
        if request_id not in entry["discovery_references"]:
            entry["discovery_references"].append(request_id)
        self.store.save_watchlist(entries)
        return key

    def _ingest(self, item, source, result, attempt, decision):
        if item["kind"] != "content" or not item.get("identity") or not item.get("url"):
            return None
        key = stable_key("instagram", item["identity"])
        content = self.store.load_content(key)
        if content is None:
            content = next((previous for previous in self.store.contents()
                            if (item.get("id") and previous.platform_content_id == item["id"])
                            or item["url"] in previous.source_url_aliases), None)
            if content is not None:
                key = content.observation.id
        if content is None:
            observation = ContentObservation(key, "instagram", item["url"], [source.id])
            content = AcquiredContent(self.context.brand_id, observation)
        observation = content.observation
        observation.source_references = sorted(set(observation.source_references + [source.id]))
        observation.retrieved_at = result.retrieved_at
        for name, value in {"creator_id": item.get("account_id"), "caption": item.get("caption"),
                            "published_at": item.get("published_at"), "duration_seconds": item.get("duration_seconds")}.items():
            if value is not None:
                setattr(observation, name, value)
        observation.content_format = item.get("content_format") or observation.content_format
        observation.domain_links = decision.domain_links
        content.platform_content_id = item.get("id") or content.platform_content_id
        content.creator_handle = item.get("handle") or content.creator_handle
        if item.get("handle"):
            content.account_reference = self.store.find_account_key(item.get("account_id"), item["handle"])
        content.source_url_aliases = sorted(set(content.source_url_aliases + [item["source_url"], item["url"]]))
        content.decision_references.append(decision.id)
        content.attempt_references.append(attempt.id)
        snapshot_id = stable_key("snapshot", source.id + key)
        if not any(s.id == snapshot_id for s in observation.performance):
            observation.performance.append(PerformanceSnapshot(snapshot_id, [source.id], RawMetrics(**item["metrics"]),
                                                               result.measured_at, "; ".join(result.limitations) or None))
        if item.get("media_url"):
            for media in content.media:
                if media.state is MediaState.LOCAL_READY and not self.store.path(media.local_path).is_file():
                    media.state, media.removed_at = MediaState.REMOVED, now()
            ready = next((m for m in content.media if m.state is MediaState.LOCAL_READY), None)
            if not ready:
                remote = next((m for m in content.media if m.state is not MediaState.REMOVED), None)
                if remote:
                    remote.remote_url = item["media_url"]
                    remote.source_references = sorted(set(remote.source_references + [source.id]))
                    remote.state = MediaState.REMOTE_ONLY
                else:
                    content.media.append(MediaReference(key, item["url"], [source.id], remote_url=item["media_url"]))
        elif not content.media:
            content.media.append(MediaReference(key, item["url"], [source.id], state=MediaState.UNAVAILABLE))
        self.store.save_content(content)
        self.summary["observations"].append(key)
        return content

    def _media(self, content, budget):
        if not budget or any(m.state is MediaState.LOCAL_READY for m in content.media):
            return False
        media = content.media[-1]
        attempt = AcquisitionAttempt(self.run_id, "media", content.observation.id, self.provider.name, AttemptStatus.RUNNING)
        self.store.save_attempt(attempt)
        self.summary["attempts"].append(attempt.id)
        self.summary["media_attempts"] += 1
        requests_before = self.downloader.request_count
        try:
            self.downloader.acquire(media)
            source = Source(self.context.brand_id, SourceType.FILE, "Acquired reference video", media.local_path,
                            metadata={"origin": "observation_reference", "canonical_url": media.source_url,
                                      "checksum_sha256": media.checksum, "retrieved_at": media.retrieved_at,
                                      "has_audio": media.has_audio})
            self.store.save_source(source)
            media.source_references.append(source.id)
            content.observation.source_references.append(source.id)
            attempt.source_references, attempt.status = [source.id], AttemptStatus.SUCCEEDED
            self.summary["media_successes"] += 1
        except AcquisitionFailure as exc:
            attempt.error, attempt.status = exc.error, AttemptStatus.FAILED
            if media.state is not MediaState.EXPIRED:
                media.state = MediaState.UNAVAILABLE
            self.summary["errors"].append({"attempt_id": attempt.id, **exc.error.to_dict()})
        attempt.ended_at = now()
        self.summary["media_download_requests"] += self.downloader.request_count - requests_before
        self.store.save_attempt(attempt)
        content.attempt_references.append(attempt.id)
        self.store.save_content(content)
        return True

    def _select_media(self, content, decision, state):
        """Coverage/resource limits only, never a performance threshold."""
        if decision.decision is not Decision.ELIGIBLE or state["media_attempts"] >= self.config.media_per_day:
            return
        account = content.account_reference or content.creator_handle or content.observation.id
        counts = state.setdefault("account_media", {})
        adjacent = all(link.relevance_level.value == "adjacent" for link in decision.domain_links)
        if counts.get(account, 0) >= self.config.media_per_account_per_day:
            return
        if adjacent and state.get("adjacent_media", 0) >= self.config.adjacent_media_per_day:
            return
        for retry in range(self.config.retries + 1):
            if state["media_attempts"] >= self.config.media_per_day or counts.get(account, 0) >= self.config.media_per_account_per_day:
                break
            if adjacent and state.get("adjacent_media", 0) >= self.config.adjacent_media_per_day:
                break
            errors_before = len(self.summary["errors"])
            if not self._media(content, True):
                break
            state["media_attempts"] += 1
            counts[account] = counts.get(account, 0) + 1
            if adjacent:
                state["adjacent_media"] = state.get("adjacent_media", 0) + 1
            self.store.write("state.json", state)
            if len(self.summary["errors"]) == errors_before or not self.summary["errors"][-1].get("retryable"):
                break
            if retry < self.config.retries:
                self.sleep(min(self.summary["errors"][-1].get("retry_after_seconds") or 2 ** retry, 30))

    def run(self):
        day = datetime.now(UTC).date().isoformat()
        state = self.store.read("state.json", {"day": day, "media_attempts": 0, "accounts_evaluated": 0, "query_offset": 0})
        if state["day"] != day:
            state.update(day=day, media_attempts=0, accounts_evaluated=0, account_media={}, adjacent_media=0)
        self.store.write(f"runs/{self.run_id}/config.json", {"schema_version": 1, "limits": {
            name: getattr(self.config, name) for name in self.config.__dataclass_fields__}})
        try:
            for seed in self.config.seed_accounts:
                seed_source = Source(self.context.brand_id, SourceType.NOTE, "Configured acquisition seed",
                                     f"runs/{self.run_id}/config.json", metadata={"origin": "configured_seed", "handle": seed["handle"],
                                                                               "domain_id": seed["domain_id"]})
                self.store.save_source(seed_source)
                self._entry({"kind": "account", "handle": seed["handle"], "id": None},
                            seed_source.id, [seed["domain_id"]])
            queries = self.config.queries
            for offset in range(min(self.config.queries_per_run, len(queries))):
                query = queries[(state["query_offset"] + offset) % len(queries)]
                request = DiscoveryRequest(self.context.brand_id, query.get("kind", "content_search"), query["term"],
                                           [query["domain_id"]], self.config.results_per_query)
                if self.context.is_domain_excluded(query["domain_id"]):
                    raise ValueError("Excluded discovery domain.")
                self.store.write(f"runs/{self.run_id}/requests/{request.id}.json", request.to_dict(), immutable=True)
                called = self._call("discover", query["term"], self.provider.discover, request=request)
                if called:
                    result, source, attempt = called
                    for item in result.items[:request.limit]:
                        self._entry(item, request.id, request.domain_ids)
                        if item["kind"] == "content" and item.get("url"):
                            decision = self._assess(item, request.domain_ids, source, "Search: " + query["term"])
                            content = self._ingest(item, source, result, attempt, decision)
                            if content:
                                self._select_media(content, decision, state)
            state["query_offset"] += self.config.queries_per_run
            entries = self.store.load_watchlist()
            ordered = sorted(entries, key=lambda k: entries[k].get("last_checked_at") or "")
            active_seen = 0
            for key in ordered:
                entries = self.store.load_watchlist()
                entry = entries[key]
                if entry["status"] in {"paused", "excluded"}:
                    continue
                try:
                    excluded = any(self.context.is_domain_excluded(domain) for domain in entry["proposed_domain_ids"])
                except KeyError:
                    entry["status"] = "paused"
                    self.summary["errors"].append({"code": "stale_domain", "account_key": key})
                    entries[key] = entry
                    self.store.save_watchlist(entries)
                    continue
                if excluded:
                    entry["status"] = "excluded"
                    entries[key] = entry
                    self.store.save_watchlist(entries)
                    continue
                if entry["status"] == "active":
                    if active_seen >= self.config.max_accounts:
                        continue
                    active_seen += 1
                if entry["status"] == "candidate" and state["accounts_evaluated"] >= self.config.new_accounts_per_day:
                    continue
                called = self._call("account", entry["handle"], self.provider.get_account, reference=entry["handle"])
                if not called:
                    continue
                result, source, attempt = called
                if not result.items or result.items[0].get("kind") != "account" or not result.items[0].get("url"):
                    continue
                item = result.items[0]
                snapshot = AccountSnapshot(key, [source.id], item.get("id"), item.get("handle"), item.get("name"),
                                           item.get("bio"), item.get("followers"), item.get("following"), item.get("post_count"),
                                           item.get("is_private"), retrieved_at=result.retrieved_at, measured_at=result.measured_at)
                self.store.write(f"accounts/{key}/snapshots/{snapshot.id}.json", snapshot.to_dict(), immutable=True)
                decision = self._assess(item, entry["proposed_domain_ids"], source, "Seed/discovered account eligibility")
                if entry["status"] == "candidate":
                    state["accounts_evaluated"] += 1
                eligible = decision.decision is Decision.ELIGIBLE and snapshot.is_private is not True
                active = sum(e["status"] == "active" for e in entries.values())
                if decision.decision is Decision.EXCLUDED:
                    entry["status"] = "excluded"
                elif eligible and (entry["status"] == "active" or active < self.config.max_accounts):
                    entry["status"] = "active"
                else:
                    entry["status"] = "candidate"
                entry["decision_references"].append(decision.id)
                entry["last_checked_at"], entry["platform_account_id"] = now(), item.get("id") or entry.get("platform_account_id")
                entries[key] = entry
                self.store.save_watchlist(entries)
                if entry["status"] != "active":
                    continue
                since = (datetime.now(UTC) - timedelta(days=self.config.history_days if not entry.get("history_collected")
                                                     else self.config.observation_days)).isoformat()
                limit = self.config.history_per_account if not entry.get("history_collected") else self.config.recent_per_account
                called = self._call("account_content", entry["handle"], self.provider.list_account_content,
                                    reference=entry["handle"], since=since, limit=limit)
                if called:
                    result, source, attempt = called
                    for item in result.items[:limit]:
                        if item["kind"] != "content" or not item.get("url"):
                            continue
                        selected = self._assess(item, entry["proposed_domain_ids"], source, "Recent/history sample from admitted account")
                        content = self._ingest(item, source, result, attempt, selected)
                        if content:
                            self._select_media(content, selected, state)
                    entries = self.store.load_watchlist()
                    entries[key]["history_collected"] = bool(result.items)
                    self.store.save_watchlist(entries)
                related = DiscoveryRequest(self.context.brand_id, "related_accounts", entry["handle"],
                                           entry["proposed_domain_ids"], self.config.results_per_query,
                                           origin_references=[decision.id])
                called = self._call("related", entry["handle"], self.provider.discover, request=related)
                if called:
                    for item in called[0].items[:related.limit]:
                        self._entry(item, related.id, related.domain_ids)
                self.store.write(f"runs/{self.run_id}/requests/{related.id}.json", related.to_dict(), immutable=True)
            self._refresh(state)
        finally:
            self.store.write("state.json", state)
            self.summary["ended_at"] = now()
            self.summary["observations"] = sorted(set(self.summary["observations"]))
            self.summary["analysis_ready"] = [c.observation.id for c in self.store.ready_for_analysis(self.context)]
            self.summary["elapsed_seconds"] = (datetime.fromisoformat(self.summary["ended_at"]) -
                                                datetime.fromisoformat(self.summary["started_at"])).total_seconds()
            self.summary["metric_coverage"] = {name: sum(content.observation.performance[-1].metrics.to_dict()[name] is not None
                                                         for content in self.store.contents() if content.observation.performance)
                                               for name in RawMetrics.__dataclass_fields__}
            self.summary["decision_counts"] = {name: sum(self.store.read(f"decisions/{reference}.json")["decision"] == name
                                                         for reference in self.summary["decisions"])
                                                for name in ("eligible", "excluded", "uncertain")}
            self.store.write(f"runs/{self.run_id}/summary.json", self.summary)
        return self.summary

    def _refresh(self, state):
        refreshed = 0
        for content in sorted(self.store.contents(), key=lambda c: c.observation.retrieved_at or ""):
            if refreshed >= self.config.metric_refreshes_per_run:
                break
            observation = content.observation
            if not observation.published_at:
                continue
            age = (datetime.now(UTC) - datetime.fromisoformat(observation.published_at.replace("Z", "+00:00"))).total_seconds() / 3600
            done = self.store.read(f"observations/{observation.id}/refresh.json", [])
            due = [h for h in self.config.refresh_hours if age >= h and h not in done]
            if not due or age > self.config.observation_days * 24 or not content.decision_references:
                continue
            previous = self.store.read(f"decisions/{content.decision_references[-1]}.json")
            if not previous or previous["decision"] != "eligible":
                continue
            called = self._call("refresh", observation.content_url, self.provider.get_content, reference=observation.content_url)
            if not called:
                continue
            refreshed += 1
            result, source, attempt = called
            matched = False
            for item in result.items:
                if item["kind"] != "content" or not item.get("url"):
                    continue
                selected = self._assess(item, [link.domain_id for link in observation.domain_links], source, "Scheduled metric refresh")
                updated = self._ingest(item, source, result, attempt, selected)
                matched = matched or (updated is not None and updated.observation.id == observation.id)
            if matched:
                self.store.write(f"observations/{observation.id}/refresh.json", sorted(set(done + due)))
