import asyncio
import hashlib
import json
import os
import unittest
from datetime import timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import AutoReconnect

from app.api import routes
from app.api.deps import get_current_user
from app.main import app
from app.models.schemas import ALL_ROLES_BY_INDUSTRY, JobListing, JobSource
from app.scrapers.source_collector import SOURCES, SourceBatch, SourceCollector, parse_posting, public_source_url
from app.scrapers import source_collector
from app.services import job_refresh
from app.services.job_refresh import JobRefreshService
from app.services.job_selection import build_response, candidate, select_jobs, utcnow
from app.services.job_store import JobStore, LeaseLost


def listing(index=0, source=JobSource.INDEED, title="Software Developer", hours=1):
    return JobListing(
        title=title, company="Example", location="New York, US", source=source,
        url=f"https://jobs.example/job/{index}", posted_at=(utcnow() - timedelta(hours=hours)).isoformat(),
    )


class SelectionTests(unittest.TestCase):
    def test_all_pdf_sources_have_identifiers(self):
        self.assertEqual(61, len(SOURCES))
        self.assertEqual(61, len({source["id"] for source in SOURCES}))
        for source in SOURCES:
            self.assertEqual(source["id"], JobSource(source["id"]).value)

    def test_global_limit_and_fair_selection(self):
        rows = []
        roles = [(industry, role) for industry, names in ALL_ROLES_BY_INDUSTRY.items() for role in names]
        for index in range(2400):
            source = JobSource.INDEED if index % 2 else JobSource.DICE
            industry, title = roles[(index // 2) % len(roles)]
            row = candidate(listing(index, source, title))
            row.update(industry=industry, role=title)
            rows.append(row)
        selected = select_jobs(rows)
        response = build_response(selected, {"snapshot_id": "test", "phase": "idle"})
        jobs = [job for roles in response.industries.values() for role in roles for job in role.jobs]
        self.assertEqual(800, len(selected))
        self.assertEqual(800, len(jobs))
        self.assertEqual(800, response.total_jobs_found)
        self.assertEqual(2, len({row["source"] for row in selected}))
        self.assertEqual(11, len({row["industry"] for row in selected}))
        self.assertEqual(800, len({job.url for job in jobs}))

    def test_per_source_role_limit(self):
        rows = [candidate(listing(index)) for index in range(30)]
        self.assertEqual(10, len(select_jobs(rows)))

    def test_duplicate_tracking_urls_and_dates(self):
        job = listing()
        duplicate = job.model_copy(update={"url": job.url + "?utm_source=test#apply"})
        self.assertEqual(1, len(select_jobs([candidate(job), candidate(duplicate)])))
        self.assertIsNone(candidate(listing(hours=49)))
        self.assertIsNone(candidate(listing(hours=-1)))
        self.assertIsNone(candidate(job.model_copy(update={"posted_at": None})))
        self.assertIsNone(candidate(job.model_copy(update={"valid_through": "2000-01-01"})))

    def test_jsonld_requires_us_eligibility_and_preserves_dates(self):
        source = {"id": "dice", "url": "https://www.dice.com"}
        data = {"title": "Software Developer", "datePosted": utcnow().isoformat(),
                "jobLocation": {"address": {"addressCountry": "US"}}}
        job = parse_posting(data, "https://www.dice.com/job/1", source)
        self.assertIsNotNone(candidate(job))
        data["jobLocation"] = {"address": {"addressCountry": "Canada"}}
        self.assertIsNone(parse_posting(data, "https://www.dice.com/job/1", source))
        self.assertFalse(public_source_url("https://dice.com.evil.test/job", source["url"]))
        self.assertFalse(public_source_url("https://dice.com@127.0.0.1/job", source["url"]))


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_remotive_us_worldwide_filter_and_publication_date(self):
        collector = SourceCollector()
        source = next(source for source in SOURCES if source["id"] == "remotive")
        jobs = [{"title": "Software Developer", "url": f"https://remotive.com/jobs/{index}",
                 "candidate_required_location": location, "publication_date": utcnow().isoformat()}
                for index, location in enumerate(["USA Only", "Worldwide", "Canada"])]
        with patch.object(source_collector, "fetch", AsyncMock(return_value=httpx.Response(200, json={"jobs": jobs}))):
            batch = await collector.collect(source)
        self.assertEqual("ok", batch.status)
        self.assertEqual(2, len(batch.jobs))
        self.assertTrue(all(candidate(job) for job in batch.jobs))

    async def test_api_keys_missing_are_visible_without_network(self):
        collector = SourceCollector()
        collector.settings = collector.settings.model_copy(update={"usajobs_api_key": None, "adzuna_app_key": None})
        with patch.object(source_collector, "fetch", AsyncMock()) as fetch:
            for source in SOURCES:
                if source["id"] in {"usajobs", "adzuna"}:
                    batch = await collector.collect(source)
                    self.assertEqual("requires_configuration", batch.status)
        fetch.assert_not_awaited()

    async def test_search_snippet_is_not_published_as_a_job(self):
        collector = SourceCollector()
        source = {**next(source for source in SOURCES if source["id"] == "dice"), "adapter": "jsonld_discovery"}
        with patch.object(source_collector, "_discover", return_value=["https://www.dice.com/job/1"]), \
             patch.object(source_collector, "fetch", AsyncMock(return_value=httpx.Response(200, text="<h1>Software Developer</h1>"))):
            batch = await collector.collect(source)
        self.assertEqual([], batch.jobs)
        self.assertEqual("error", batch.status)

    async def test_native_role_hint_keeps_provider_matched_abbreviations(self):
        job = listing(title="Staff Nurse Specialist")
        self.assertIsNone(candidate(job))
        self.assertEqual("Registered Nurse", candidate(job, "Registered Nurse")["role"])


@unittest.skipUnless(os.environ.get("JOB_TEST_MONGODB_URL"), "Requires isolated test MongoDB")
class MongoIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(os.environ["JOB_TEST_MONGODB_URL"], tz_aware=True,
                                        serverSelectionTimeoutMS=5000)
        self.database = self.client["job_scheduler_test_" + uuid4().hex]
        self.store = JobStore(self.database)
        await self.store.ensure_indexes()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver")
        app.dependency_overrides[get_current_user] = lambda: {"email": "test@example.com"}
        app.dependency_overrides[routes.get_job_store] = lambda: self.store

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.http.aclose()
        await self.client.drop_database(self.database.name)
        self.client.close()

    async def publish(self, rows=None, statuses=None):
        owner = uuid4().hex
        self.assertIsNotNone(await self.store.claim(owner))
        await self.store.publish(owner, rows if rows is not None else [candidate(listing())], statuses or {})
        await self.store.finish(owner, delay=0)

    async def test_api_returns_saved_jobs_without_collecting(self):
        await self.publish()
        with patch("app.scrapers.source_collector.SourceCollector.collect", AsyncMock(side_effect=AssertionError("API must only read"))):
            response = await asyncio.wait_for(self.http.get("/api/v1/jobs/scrape-all"), timeout=1)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["total_jobs_found"])
        self.assertTrue(response.json()["ready"])
        self.assertTrue(response.json()["last_updated_at"])
        # A new store/client-facing request sees durable data too.
        self.assertEqual(1, (await JobStore(self.database).response()).total_jobs_found)

    async def test_first_request_empty_and_auth_is_required(self):
        response = await self.http.get("/api/v1/jobs/scrape-all")
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, response.json()["total_jobs_found"])
        self.assertFalse(response.json()["ready"])
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(401, (await self.http.get("/api/v1/jobs/scrape-all")).status_code)

    async def test_old_snapshot_remains_visible_until_publish(self):
        await self.publish()
        owner = uuid4().hex
        await self.store.claim(owner)
        before = await self.store.response()
        self.assertEqual(1, before.total_jobs_found)
        self.assertEqual("scraping", before.refresh_status)
        await self.store.publish(owner, [candidate(listing(2)), candidate(listing(3))], {})
        self.assertEqual(2, (await self.store.response()).total_jobs_found)

    async def test_competing_scheduler_and_expired_owner_are_fenced(self):
        await self.publish()
        self.assertIsNotNone(await self.store.claim("first"))
        self.assertIsNone(await self.store.claim("second"))
        await self.store.renew("first")
        await self.store.state.update_one({"_id": "all"}, {"$set": {"lease_until": utcnow() - timedelta(seconds=1)}})
        self.assertIsNotNone(await self.store.claim("second"))
        with self.assertRaises(LeaseLost):
            await self.store.renew("first")
        with self.assertRaises(LeaseLost):
            await self.store.publish("first", [candidate(listing(4))], {})
        self.assertEqual(1, (await self.store.response()).total_jobs_found)
        await self.store.finish("first", 0)
        self.assertEqual("second", (await self.store.metadata())["owner"])

    async def test_partial_database_write_preserves_active_pointer(self):
        await self.publish()
        previous = (await self.store.metadata())["snapshot_id"]
        await self.store.claim("refresh")
        with patch.object(self.store.rows, "bulk_write", AsyncMock(side_effect=AutoReconnect("test"))):
            with self.assertRaises(AutoReconnect):
                await self.store.publish("refresh", [candidate(listing(3))], {})
        self.assertEqual(previous, (await self.store.metadata())["snapshot_id"])

    async def test_ambiguous_batch_ack_retries_without_duplicate_rows(self):
        await self.store.claim("worker")
        original = self.store.rows.bulk_write
        calls = 0
        async def write_then_timeout(operations, **kwargs):
            nonlocal calls
            calls += 1
            result = await original(operations, **kwargs)
            if calls == 1:
                raise AutoReconnect("ack lost after commit")
            return result
        rows = [candidate(listing(index)) for index in range(60)]
        with patch.object(self.store.rows, "bulk_write", write_then_timeout):
            await self.store.publish("worker", rows, {})
        self.assertEqual(4, calls)
        self.assertEqual(60, (await self.store.response()).total_jobs_found)
        self.assertEqual(60, await self.store.rows.count_documents({}))

    async def test_expired_rows_are_filtered_at_read_time(self):
        rows = [candidate(listing(index)) for index in range(3)]
        rows[1]["published_at"] = utcnow() - timedelta(hours=49)
        rows[2]["expires_at"] = utcnow() - timedelta(seconds=1)
        await self.publish(rows)
        self.assertEqual(1, (await self.store.response()).total_jobs_found)

    async def test_processed_urls_persist_for_unprocessed_endpoint(self):
        await self.publish()
        response = await self.http.get("/api/v1/jobs/unprocessed?category=IT")
        self.assertEqual(1, response.json()["total_unprocessed"])
        url = listing().url
        marked = await self.http.patch("/api/v1/jobs/mark-processed", json={"urls": [url, url, ""]})
        self.assertEqual(1, marked.json()["marked"])
        self.assertEqual(0, (await self.store.mark_processed([url]))[0])
        self.assertEqual({url}, await JobStore(self.database).known_urls([url]))
        duplicates = await self.http.post("/api/v1/jobs/check-duplicates", json={"urls": [url, "new"]})
        self.assertEqual([url], duplicates.json()["duplicates"])
        self.assertEqual(0, (await self.http.get("/api/v1/jobs/unprocessed?category=IT")).json()["total_unprocessed"])

    async def test_refresh_retains_previous_jobs_when_source_fails(self):
        await self.publish()
        service = JobRefreshService(self.store)
        service.settings = service.settings.model_copy(update={"enrich_decision_makers": False})
        with patch.object(job_refresh, "SOURCES", [SOURCES[0]]), \
             patch.object(service.collector, "collect", AsyncMock(return_value=SourceBatch("indeed", status="error", error="blocked"))):
            self.assertTrue(await service.run_once("refresh"))
        response = await self.store.response()
        self.assertEqual(1, response.total_jobs_found)
        self.assertEqual("error", response.source_statuses["indeed"]["status"])
        self.assertIsNone(await self.store.claim("too-soon"))

    async def test_new_jobs_persist_and_scheduled_feed_cache_is_shared(self):
        now = utcnow()
        statuses = {"remotive": {"last_successful_sync_at": now, "status": "ok",
                    "configuration_signature": hashlib.sha256(json.dumps(next(s for s in SOURCES if s["id"] == "remotive"), sort_keys=True).encode()).hexdigest()}}
        await self.publish([candidate(listing(source=JobSource.REMOTIVE))], statuses)
        remotive = next(source for source in SOURCES if source["id"] == "remotive")
        service = JobRefreshService(self.store)
        service.settings = service.settings.model_copy(update={"enrich_decision_makers": False})
        with patch.object(job_refresh, "SOURCES", [remotive]), \
             patch.object(service.collector, "collect", AsyncMock()) as collect:
            await service.run_once("refresh")
        collect.assert_not_awaited()
        self.assertEqual(1, (await self.store.response()).total_jobs_found)
        self.assertEqual("cached", (await self.store.response()).source_statuses["remotive"]["status"])

    async def test_cancellation_cancels_collection_and_releases_lease(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def collect(source):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        service = JobRefreshService(self.store)
        with patch.object(job_refresh, "SOURCES", [SOURCES[0]]), patch.object(service.collector, "collect", collect):
            task = asyncio.create_task(service.run_once("worker"))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(stopped.is_set())
        self.assertNotIn("owner", await self.store.metadata())
        self.assertEqual("error", (await self.store.metadata())["phase"])

    async def test_simultaneous_initial_claim_has_one_owner(self):
        claims = await asyncio.gather(self.store.claim("one"), self.store.claim("two"))
        self.assertEqual(1, sum(claim is not None for claim in claims))

    async def test_scheduler_pipeline_stores_new_collected_jobs(self):
        service = JobRefreshService(self.store)
        service.settings = service.settings.model_copy(update={"enrich_decision_makers": False})
        with patch.object(job_refresh, "SOURCES", [SOURCES[0]]), \
             patch.object(service.collector, "collect", AsyncMock(return_value=SourceBatch("indeed", jobs=[listing(77)]))):
            await service.run_once("worker")
        response = await self.http.get("/api/v1/jobs/scrape-all")
        self.assertEqual(1, response.json()["total_jobs_found"])
        self.assertEqual("ok", response.json()["source_statuses"]["indeed"]["status"])
        self.assertEqual(listing(77).url, response.json()["industries"]["Information Technology"][5]["jobs"][0]["url"])

    async def test_preloaded_api_cache_serves_without_database_io(self):
        await self.publish()
        cached = JobStore(self.database, cached_reads=True)
        await cached.refresh_cache()
        app.dependency_overrides[routes.get_job_store] = lambda: cached
        with patch.object(cached, "metadata", AsyncMock(side_effect=AssertionError("request read DB"))), \
             patch.object(cached, "active_rows", AsyncMock(side_effect=AssertionError("request fetched rows"))):
            response = await self.http.get("/api/v1/jobs/scrape-all")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["total_jobs_found"])

    async def test_cache_swap_retains_previous_result_on_partial_load_failure(self):
        await self.publish()
        cached = JobStore(self.database, cached_reads=True)
        await cached.refresh_cache()
        await self.publish([candidate(listing(3)), candidate(listing(4))])
        self.assertEqual(1, (await cached.response()).total_jobs_found)
        with patch.object(cached, "active_rows", AsyncMock(side_effect=AutoReconnect("test"))):
            with self.assertRaises(AutoReconnect):
                await cached.refresh_cache()
        self.assertEqual(1, (await cached.response()).total_jobs_found)
        await cached.refresh_cache()
        self.assertEqual(2, (await cached.response()).total_jobs_found)

    async def test_cached_jobs_still_expire_on_request(self):
        await self.publish()
        cached = JobStore(self.database, cached_reads=True)
        await cached.refresh_cache()
        with patch("app.services.job_selection.utcnow", return_value=utcnow() + timedelta(hours=49)):
            self.assertEqual(0, (await cached.response()).total_jobs_found)

    async def test_database_failure_returns_503_without_scraping(self):
        with patch.object(self.store, "response", AsyncMock(side_effect=AutoReconnect("test"))):
            response = await self.http.get("/api/v1/jobs/scrape-all")
        self.assertEqual(503, response.status_code)


if __name__ == "__main__":
    unittest.main()
