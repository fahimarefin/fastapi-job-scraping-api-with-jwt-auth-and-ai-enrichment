"""Scheduled collection, selection and atomic publication of ready job data."""
from __future__ import annotations

import asyncio
import logging
import hashlib
import json
from collections import Counter
from datetime import timedelta

from app.core.config import get_settings
from app.models.schemas import JobListing
from app.scrapers.enrichment_service import enricher
from app.scrapers.source_collector import ALL_SOURCES as SOURCES, SourceCollector
from app.services.job_selection import candidate, parse_date, publication_date, select_jobs, utcnow
from app.services.job_store import JobStore, LeaseLost

logger = logging.getLogger(__name__)


class JobRefreshService:
    def __init__(self, store: JobStore):
        self.store = store
        self.settings = get_settings()
        self.collector = SourceCollector()

    async def refresh(self, owner: str):
        metadata = await self.store.metadata() or {}
        previous = await self.store.active_rows(metadata)
        statuses = dict(metadata.get("source_statuses", {}))
        fresh_rows = []
        sem = asyncio.Semaphore(self.settings.job_source_concurrency)

        async def source_task(source):
            source_id = source["id"]
            prior = statuses.get(source_id, {})
            signature = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
            last_success = parse_date(prior.get("last_successful_sync_at"))
            interval = source.get("refresh_interval_seconds", self.settings.job_refresh_interval_seconds)
            if prior.get("configuration_signature") == signature and last_success and utcnow() < last_success + timedelta(seconds=interval):
                statuses[source_id] = {**prior, "status": "cached"}
                return
            async with sem:
                started = utcnow()
                batch = await self.collector.collect(source)
                accepted = []
                rejected = Counter()
                for job in batch.jobs:
                    row = candidate(job, batch.role_hints.get(job.url))
                    if row:
                        accepted.append(row)
                        continue
                    posted = publication_date(job)
                    if posted is None:
                        rejected["unverified_publication_date"] += 1
                    elif not utcnow() - timedelta(hours=48) <= posted <= utcnow():
                        rejected["outside_48_hour_window"] += 1
                    elif parse_date(job.valid_through) and parse_date(job.valid_through) <= utcnow():
                        rejected["expired"] += 1
                    else:
                        rejected["unmatched_role_or_invalid_url"] += 1
                fresh_rows.extend(accepted)
                successful = batch.status == "ok"
                statuses[source_id] = {
                    "status": batch.status, "adapter": source["adapter"], "url": source["url"],
                    "configuration_signature": signature,
                    "last_attempt_at": started,
                    "last_successful_sync_at": utcnow() if successful else last_success,
                    "collected": len(batch.jobs), "accepted": len(accepted),
                    "error": batch.error, "rejected": dict(rejected), "diagnostics": batch.diagnostics,
                }
                logger.info("Source %s finished with %d accepted jobs (%s)", source_id, len(accepted), batch.status)

        # One task per board avoids a source × role explosion for discovery.
        async with asyncio.TaskGroup() as group:
            for source in SOURCES:
                group.create_task(source_task(source))

        # New observations replace older copies of the same canonical URL.
        merged = {row["job_key"]: row for row in previous}
        merged.update({row["job_key"]: row for row in fresh_rows})
        rows = select_jobs(list(merged.values()))
        selected_counts = Counter(row["source"] for row in rows)
        for source_id, status in statuses.items():
            status["selected"] = selected_counts[source_id]
        jobs = [JobListing.model_validate(row["job"]) for row in rows]
        if self.settings.enrich_decision_makers:
            # Reuse saved company enrichment across worker restarts too.
            companies = {row["job"].get("company"): row["job"] for row in previous
                         if row["job"].get("company_info")}
            needs_enrichment = []
            for job in jobs:
                saved = companies.get(job.company)
                if saved:
                    cached = JobListing.model_validate(saved)
                    job.company_info = cached.company_info
                    job.decision_maker = cached.decision_maker
                    job.decision_makers = cached.decision_makers
                else:
                    needs_enrichment.append(job)
            await enricher.enrich_listings(needs_enrichment, max_contacts=self.settings.max_decision_makers_per_company)
        for row, job in zip(rows, jobs):
            row["job"] = job.model_dump(mode="json")
        await self.store.publish(owner, rows, statuses)
        logger.info("Published %d unique jobs from %d source attempts", len(rows), len(statuses))

    async def run_once(self, owner: str) -> bool:
        if await self.store.claim(owner) is None:
            return False
        error = None
        async def heartbeat():
            while True:
                await asyncio.sleep(30)
                await self.store.renew(owner)
        refresh = asyncio.create_task(self.refresh(owner))
        renewal = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait({refresh, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if refresh in done:
                await refresh
            else:
                await renewal
                raise LeaseLost("Scheduler heartbeat stopped")
        except asyncio.CancelledError:
            error = "Refresh interrupted. Retained the previous published result."
            raise
        except Exception:
            error = "Refresh failed. Retained the previous published result."
            logger.exception("Job refresh failed")
        finally:
            refresh.cancel()
            renewal.cancel()
            await asyncio.gather(refresh, renewal, return_exceptions=True)
            delay = 60 if error else self.settings.job_refresh_interval_seconds
            await self.store.finish(owner, delay=delay, error=error)
        return True
