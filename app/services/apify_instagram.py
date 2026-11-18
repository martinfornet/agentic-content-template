"""Apify Instagram adapter. Actor details never escape into intelligence logic."""
from __future__ import annotations
from datetime import datetime, UTC
import math
import time
from urllib.parse import quote, urlparse

from app.models.acquisition import AcquisitionError, AcquisitionFailure, now
from app.services.acquisition_provider import ProviderResult
from app.services.acquisition_http import JsonHttp


def known_count(value):
    return value if type(value) is int and value >= 0 else None


def reported_cost(run):
    value = run.get("usageTotalUsd")
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def observed_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def normalize_item(raw):
    """Literal supported fields only; exact payload is retained separately."""
    if raw.get("error"):
        return {"kind": "failure", "url": raw.get("inputUrl"), "raw": raw,
                "error": {"code": "content_unavailable", "message": "Provider reported unavailable content; specific reason unverified."}}
    handle = raw.get("ownerUsername") or raw.get("username")
    account_id = raw.get("ownerId")
    content_id = raw.get("id")
    shortcode = raw.get("shortCode")
    is_content = bool(shortcode or raw.get("videoUrl") or raw.get("videoPlayCount") is not None
                      or raw.get("productType") == "clips")
    if is_content:
        if not shortcode:
            parts = urlparse(raw.get("url", "")).path.strip("/").split("/")
            shortcode = parts[1] if len(parts) >= 2 and parts[0] in {"p", "reel"} else None
        canonical = f"https://www.instagram.com/reel/{shortcode}/" if shortcode else raw.get("url")
        duration = raw.get("videoDuration")
        metrics = {"likes": known_count(raw.get("likesCount")), "comments": known_count(raw.get("commentsCount")),
                   "views": known_count(raw.get("videoViewCount")), "plays": known_count(raw.get("videoPlayCount")),
                   "shares": known_count(raw.get("sharesCount")), "saves": None, "sends": None, "reach": None}
        return {"kind": "content", "id": str(content_id) if content_id is not None else None,
                "identity": shortcode or (str(content_id) if content_id is not None else canonical),
                "url": canonical, "source_url": raw.get("url") or canonical,
                "account_id": str(account_id) if account_id is not None else None, "handle": handle,
                "caption": raw.get("caption") or None, "published_at": observed_time(raw.get("timestamp")),
                "duration_seconds": duration if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0 else None,
                "content_format": "reel" if raw.get("productType") == "clips" else
                    "video" if raw.get("videoUrl") or raw.get("type") == "Video" else (raw.get("type") or "unknown").lower(),
                "media_url": raw.get("videoUrl") or raw.get("downloadedVideoUrl"),
                "metrics": metrics, "raw": raw}
    handle = raw.get("username") or raw.get("userName")
    return {"kind": "account", "id": str(content_id) if content_id is not None else None, "handle": handle,
            "url": raw.get("url") or (f"https://www.instagram.com/{handle}/" if handle else None),
            "name": raw.get("fullName"), "bio": raw.get("biography"),
            "followers": known_count(raw.get("followersCount")), "following": known_count(raw.get("followsCount")),
            "post_count": known_count(raw.get("postsCount")),
            "is_private": raw.get("private") if type(raw.get("private")) is bool else None, "raw": raw}


class ApifyInstagramProvider:
    name = "apify_instagram"
    base = "https://api.apify.com/v2"

    def __init__(self, token, *, http=None, include_shares=False, wait_seconds=180, poll_seconds=5,
                 sleep=time.sleep, monotonic=time.monotonic):
        if not token:
            raise AcquisitionFailure(AcquisitionError("authentication_failure", "Configure APIFY_TOKEN before acquisition."))
        self.headers = {"Authorization": "Bearer " + token}
        self.http = http or JsonHttp()
        self.include_shares = include_shares
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds
        self.sleep, self.monotonic = sleep, monotonic

    def _run(self, actor, payload, *, job_id=None, on_job=None):
        if not job_id:
            created = self.http.request("POST", f"{self.base}/acts/{actor.replace('/', '~')}/runs",
                                        headers=self.headers, body=payload)
            try:
                job_id = created["data"]["id"]
            except (KeyError, TypeError):
                raise AcquisitionFailure(AcquisitionError("invalid_response", "Actor start response has no job id."))
            if on_job:
                on_job(job_id)
        deadline = self.monotonic() + self.wait_seconds
        while True:
            try:
                run = self.http.request("GET", f"{self.base}/actor-runs/{quote(job_id, safe='')}", headers=self.headers)["data"]
                status = run["status"]
            except AcquisitionFailure as exc:
                exc.error.provider_job_id = job_id
                raise
            except (KeyError, TypeError):
                raise AcquisitionFailure(AcquisitionError("invalid_response", "Invalid actor run response.", provider_job_id=job_id))
            if status == "SUCCEEDED":
                break
            if status in {"FAILED", "TIMED-OUT", "ABORTED"}:
                raise AcquisitionFailure(AcquisitionError("provider_failure", f"Actor ended with status {status}.",
                                                         provider_job_id=job_id, provider_cost_usd=reported_cost(run)))
            if self.monotonic() >= deadline:
                raise AcquisitionFailure(AcquisitionError("provider_pending", "Actor still running; resume this job rather than start another.",
                                                         True, provider_job_id=job_id))
            self.sleep(self.poll_seconds)
        try:
            rows = self.http.request("GET", f"{self.base}/datasets/{quote(run['defaultDatasetId'], safe='')}/items?format=json&clean=true",
                                     headers=self.headers)
        except AcquisitionFailure as exc:
            exc.error.provider_job_id = job_id
            exc.error.provider_cost_usd = reported_cost(run)
            raise
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AcquisitionFailure(AcquisitionError("invalid_response", "Dataset is not a list of objects.",
                                                     provider_job_id=job_id, provider_cost_usd=reported_cost(run)))
        cost = reported_cost(run)
        return ProviderResult([normalize_item(row) for row in rows], {"items": rows, "run": run}, now(), job_id,
                              limitations=["Measurement age unknown; provider counts may be cached.",
                                           "Private metrics unavailable; missing/sentinel values remain unknown."],
                              cost_usd=cost)

    def discover(self, request, *, job_id=None, on_job=None):
        if request.kind == "related_accounts":
            profile = self.get_account(request.term_or_account_reference, job_id=job_id, on_job=on_job)
            raw = profile.raw["items"]
            related = [row for item in raw for row in item.get("relatedProfiles", [])][:request.limit]
            # Related-profile keys use the platform's snake_case schema.
            items = [normalize_item({"id": row.get("id"), "username": row.get("username"),
                                     "fullName": row.get("full_name"), "private": row.get("is_private")}) for row in related]
            return ProviderResult(items, profile.raw, profile.retrieved_at, profile.job_id,
                                  limitations=["Related-account suggestions are incomplete and not relevance decisions."],
                                  cost_usd=profile.cost_usd)
        return self._run("apify/instagram-search-scraper",
                         {"search": request.term_or_account_reference,
                          "searchType": "popular" if request.kind == "content_search" else "user",
                          "searchLimit": min(request.limit, 250)}, job_id=job_id, on_job=on_job)

    def get_account(self, reference, *, job_id=None, on_job=None):
        return self._run("apify/instagram-profile-scraper", {"usernames": [reference], "includeAboutSection": False},
                         job_id=job_id, on_job=on_job)

    def report_usage(self, job_ids):
        """Read current billing snapshots; completion-time charges may lag."""
        jobs = []
        for job_id in sorted(set(job_ids)):
            try:
                run = self.http.request("GET", f"{self.base}/actor-runs/{quote(job_id, safe='')}", headers=self.headers)["data"]
                jobs.append({"job_id": job_id, "status": run.get("status"), "cost_usd": reported_cost(run),
                             "charged_events": run.get("chargedEventCounts")})
            except AcquisitionFailure as exc:
                jobs.append({"job_id": job_id, "cost_usd": None, "error": exc.error.to_dict()})
            except (KeyError, TypeError, AttributeError):
                jobs.append({"job_id": job_id, "cost_usd": None,
                             "error": {"code": "invalid_response", "message": "Usage response unavailable."}})
        known = [j["cost_usd"] for j in jobs if j["cost_usd"] is not None]
        return {"checked_at": now(), "jobs": jobs, "reported_cost_usd": sum(known) if known else None,
                "unreported_jobs": len(jobs) - len(known),
                "limitations": "Provider usage snapshot, not a final invoice; charges may update later."}

    def list_account_content(self, reference, *, since, limit, cursor=None, job_id=None, on_job=None):
        if cursor is not None:
            raise AcquisitionFailure(AcquisitionError("unsupported_pagination", "Reel actor does not expose a cursor through this adapter."))
        # Actor schema accepts UTC Z but rejects ISO numeric timezone offsets.
        cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
        provider_since = cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z") if cutoff.tzinfo else since
        result = self._run("apify/instagram-reel-scraper",
                          {"username": [reference], "resultsLimit": limit, "onlyPostsNewerThan": provider_since,
                           "skipPinnedPosts": True, "includeTranscript": False, "includeDownloadedVideo": False,
                           "includeSharesCount": self.include_shares}, job_id=job_id, on_job=on_job)
        result.limitations.append("Bounded history sample; completeness is not guaranteed. Pinned items skipped.")
        return result

    def get_content(self, reference, *, job_id=None, on_job=None):
        return self.list_account_content(reference, since="1970-01-01", limit=1, job_id=job_id, on_job=on_job)
