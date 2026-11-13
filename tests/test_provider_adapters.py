import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.scrapers.provider_adapters import ProviderAdapters, ProviderError, rpc_payload, remote_us
from app.scrapers.job_parsing import SourceBatch
from app.scrapers.source_collector import SOURCES
from app.services.job_selection import utcnow, candidate


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_ashby_null_addresses_and_us_eligibility(self):
        adapter = ProviderAdapters()
        source = next(s for s in SOURCES if s['id'] == 'zapier')
        base = dict(title='Software Developer', jobUrl='https://jobs.ashbyhq.com/zapier/1',
                    publishedAt=utcnow().isoformat(), location='United States', isListed=True)
        rows = [dict(base, address=None, secondaryLocations=[{'address': None}]),
                dict(base, address={'postalAddress': {'addressCountry': 'US'}}),
                dict(base, address={'postalAddress': {'addressCountry': 'CA'}})]
        batch = SourceBatch('zapier')
        with patch.object(adapter, '_get', AsyncMock(return_value=httpx.Response(200, json={'jobs': rows}))):
            await adapter.collect(source, batch)
        self.assertEqual(1, len(batch.jobs))
        self.assertIsNotNone(candidate(batch.jobs[0]))

    async def test_greenhouse_uses_original_publication_not_update(self):
        adapter = ProviderAdapters()
        source = next(s for s in SOURCES if s['id'] == 'gitlab')
        row = dict(title='Software Developer', absolute_url='https://job-boards.greenhouse.io/gitlab/jobs/1',
                   location={'name': 'United States'}, updated_at=utcnow().isoformat(), first_published='2020-01-01')
        batch = SourceBatch('gitlab')
        with patch.object(adapter, '_get', AsyncMock(return_value=httpx.Response(200, json={'jobs': [row]}))):
            await adapter.collect(source, batch)
        self.assertEqual(1, len(batch.jobs))
        self.assertIsNone(candidate(batch.jobs[0]))

    def test_provider_allowlist(self):
        adapter = ProviderAdapters()
        source = next(s for s in SOURCES if s['id'] == 'gitlab')
        adapter._allowed(source['feed_url'], source)
        for url in ['https://127.0.0.1/jobs', 'https://boards-api.greenhouse.io.evil.test/jobs',
                    'http://boards-api.greenhouse.io/jobs', 'https://user@boards-api.greenhouse.io/jobs']:
            with self.assertRaises(ProviderError):
                adapter._allowed(url, source)

    def test_remote_eligibility_and_mcp_stream(self):
        self.assertTrue(remote_us('Anywhere in the World'))
        self.assertFalse(remote_us('Asia Only'))
        self.assertFalse(remote_us(''))
        response = httpx.Response(200, headers={'content-type': 'text/event-stream'},
            text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"data":[]}}\n\n',
            request=httpx.Request('POST', 'https://mcp.dice.com/mcp'))
        self.assertEqual({'data': []}, rpc_payload(response)['result'])
