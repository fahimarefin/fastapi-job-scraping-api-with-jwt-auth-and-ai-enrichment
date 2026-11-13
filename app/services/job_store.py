"""Mongo-backed job snapshots and a renewable scheduler lease."""
from __future__ import annotations

import asyncio
import logging

from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from pymongo import ASCENDING, ReplaceOne, ReturnDocument, UpdateOne
from pymongo.errors import AutoReconnect, PyMongoError

from app.core.database import get_database
from app.models.schemas import MAX_TOTAL_JOBS
from app.services.job_selection import build_response, utcnow

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


class LeaseLost(RuntimeError):
    pass


class JobStore:
    def __init__(self, database=None, cached_reads: bool = False):
        self.database = database if database is not None else get_database()
        self.state = self.database["job_refresh_state"]
        self.rows = self.database["job_snapshot_rows"]
        self.processed = self.database["processed_job_urls"]
        self.cached_reads = cached_reads
        self._cached_metadata = None
        self._cached_rows = []
        self._cache_error = None

    async def ensure_indexes(self):
        await self.rows.create_index([("snapshot_id", ASCENDING), ("position", ASCENDING)])
        # Retain immutable generations long enough for requests which read an
        # older active pointer while a refresh publishes its replacement.
        await self.rows.create_index("created_at", expireAfterSeconds=259200)

    async def metadata(self):
        return await self.state.find_one({"_id": "all"}, max_time_ms=3000)

    async def active_rows(self, metadata=None):
        metadata = metadata if metadata is not None else await self.metadata()
        if not metadata or not metadata.get("snapshot_id"):
            return []
        now = utcnow()
        query = {"snapshot_id": metadata["snapshot_id"],
                 "published_at": {"$gte": now - timedelta(hours=48), "$lte": now},
                 "$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}]}
        cursor = self.rows.find(query, {"_id": 0}).sort("position", ASCENDING)
        # Bound each network reply on slower database links.
        return await cursor.batch_size(25).limit(MAX_TOTAL_JOBS).max_time_ms(3000).to_list(length=MAX_TOTAL_JOBS)

    async def response(self):
        if not self.cached_reads:
            metadata = await self.metadata()
            return build_response(await self.active_rows(metadata), metadata)
        if self._cache_error and self._cached_metadata is None:
            raise self._cache_error
        response = build_response(self._cached_rows, self._cached_metadata)
        if self._cache_error:
            response.refresh_error = "Database cache refresh is temporarily unavailable"
        return response

    async def refresh_cache(self):
        metadata = await self.metadata()
        generation = metadata.get("snapshot_id") if metadata else None
        previous = self._cached_metadata.get("snapshot_id") if self._cached_metadata else None
        rows = self._cached_rows
        if generation != previous:
            rows = await self.active_rows(metadata)
        # Swap complete generations together, leaving the previous cache readable
        # throughout network I/O and when a refresh fails partway.
        self._cached_rows, self._cached_metadata = rows, metadata
        self._cache_error = None

    async def run_cache(self):
        while True:
            try:
                await self.refresh_cache()
            except PyMongoError as exc:
                self._cache_error = exc
                logger.warning("Job cache refresh failed (%s)", type(exc).__name__)
            await asyncio.sleep(10)

    async def claim(self, owner: str):
        now = utcnow()
        await self.state.update_one({"_id": "all"}, {"$setOnInsert": {
            "lease_until": now, "next_due_at": now, "phase": "warming_up",
        }}, upsert=True)
        return await self.state.find_one_and_update(
            {"_id": "all", "lease_until": {"$lte": now}, "next_due_at": {"$lte": now}},
            {"$set": {"owner": owner, "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                      "phase": "scraping", "started_at": now, "last_error": None}},
            return_document=ReturnDocument.AFTER,
        )

    async def renew(self, owner: str):
        now = utcnow()
        result = await self.state.update_one(
            {"_id": "all", "owner": owner, "lease_until": {"$gt": now}},
            {"$set": {"lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
        )
        if not result.matched_count:
            raise LeaseLost("Scheduler lease expired or was acquired by another worker")

    async def publish(self, owner: str, rows: list[dict], source_statuses: dict):
        if len(rows) > MAX_TOTAL_JOBS:
            raise ValueError("Snapshot exceeds the 800-job limit")
        generation = uuid4().hex
        created_at = utcnow()
        documents = [{**row, "_id": f'{generation}:{row["job_key"]}',
                      "snapshot_id": generation, "position": position, "created_at": created_at}
                     for position, row in enumerate(rows)]
        # Small idempotent batches tolerate slow links and ambiguous write
        # acknowledgements without producing duplicates on retry.
        for offset in range(0, len(documents), 25):
            batch = documents[offset:offset + 25]
            operations = [ReplaceOne({"_id": document["_id"]}, document, upsert=True) for document in batch]
            for attempt in range(3):
                try:
                    await self.rows.bulk_write(operations, ordered=False)
                    break
                except AutoReconnect:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(attempt + 1)
        # The pointer and lease occupy one document. This conditional write
        # prevents an expired worker from publishing over its replacement.
        result = await self.state.update_one(
            {"_id": "all", "owner": owner, "lease_until": {"$gt": utcnow()}},
            {"$set": {"snapshot_id": generation, "published_at": created_at,
                      "source_statuses": source_statuses, "phase": "published"}},
        )
        if not result.matched_count:
            raise LeaseLost("Snapshot publication rejected after lease loss")

    async def finish(self, owner: str, delay: int, error: str | None = None):
        now = utcnow()
        await self.state.update_one(
            {"_id": "all", "owner": owner},
            {"$set": {"lease_until": now, "next_due_at": now + timedelta(seconds=delay),
                      "phase": "error" if error else "idle", "last_error": error,
                      "finished_at": now}, "$unset": {"owner": ""}},
        )

    @staticmethod
    def _url_id(url: str):
        return sha256(url.encode()).hexdigest()

    async def known_urls(self, urls: list[str]) -> set[str]:
        ids = [self._url_id(url) for url in urls if url]
        documents = await self.processed.find({"_id": {"$in": ids}}, {"url": 1}).to_list(length=None)
        return {document["url"] for document in documents}

    async def mark_processed(self, urls: list[str]) -> tuple[int, int]:
        valid = list(dict.fromkeys(url for url in urls if url))
        marked = 0
        if valid:
            result = await self.processed.bulk_write([UpdateOne(
                {"_id": self._url_id(url)},
                {"$setOnInsert": {"url": url, "processed_at": utcnow()}}, upsert=True,
            ) for url in valid], ordered=False)
            marked = result.upserted_count
        return marked, await self.processed.count_documents({})
