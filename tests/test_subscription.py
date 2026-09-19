"""Subscription rendering and browser/client negotiation regressions."""
import asyncio
import base64
from html import unescape
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "api"))

from fastapi import FastAPI
import httpx
from emunel_console.services import gateway
from emunel_console.services.subscription import render_subscription

CONFIGS = [{"protocol": "vless-ws", "label": "Example", "share_url":
    "vless://00000000-0000-0000-0000-000000000001@example.test:443?security=tls&type=ws&path=%2Fi%2Ftoken%2Fws%2Fuuid&alpn=http%2F1.1#Example"}]


class SubscriptionTests(unittest.TestCase):
    def test_template_and_escaping(self):
        configs = [dict(CONFIGS[0], label='<script>alert(1)</script>')]
        page = render_subscription('<script>alert(1)</script>@@COUNT@@', configs, 'example.test', '/i/token/sub')
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;@@COUNT@@', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('buildQrSvg', page)
        self.assertNotIn('cdnjs.cloudflare.com', page)
        self.assertNotIn('<?', page)
        self.assertIn('Not reported', page)
        self.assertIn('/i/token/ws/uuid', page)
        self.assertIn('http/1.1', page)
        self.assertIn(CONFIGS[0]['share_url'], unescape(page))
        matrix = json.loads(re.search(r'var QR_MATRIX = (.*);', page)[1])
        self.assertGreater(len(matrix), 20)
        self.assertTrue(all(len(row) == len(matrix) for row in matrix))

    def test_vmess_exports_and_page(self):
        data = {'v': '2', 'ps': 'VMess test', 'id': '123e4567-e89b-12d3-a456-426614174000',
                'add': 'example.test', 'port': '443', 'aid': '0', 'scy': 'auto', 'net': 'ws',
                'tls': 'tls', 'path': '/i/token/vmess-ws/uuid'}
        url = 'vmess://' + base64.b64encode(json.dumps(data).encode()).decode()
        self.assertEqual(gateway._singbox_outbound(url)['type'], 'vmess')
        self.assertEqual(gateway._clash_proxy(url)['network'], 'ws')
        page = render_subscription('VMess', [{'share_url': url}], 'example.test', '/i/token/sub')
        self.assertIn('VMess test', page)
        self.assertIn('/i/token/vmess-ws/uuid', page)
        self.assertIn(url, page)

    def test_xhttp_export_is_not_mislabeled_websocket(self):
        from fastapi import HTTPException
        url = CONFIGS[0]['share_url'].replace('type=ws', 'type=xhttp')
        for export in (gateway._singbox_outbound, gateway._clash_proxy):
            with self.assertRaises(HTTPException) as caught:
                export(url)
            self.assertEqual(caught.exception.status_code, 422)

    def test_page_renders_only_real_configs(self):
        """The channel promo card is gone — a subscription page shows exactly
        the instance's own configs, nothing injected."""
        page = render_subscription('Mobile', CONFIGS, 'example.test', '/i/token/sub')
        self.assertNotIn('t.me', page)
        self.assertNotIn('channel-note', page)
        self.assertIn(CONFIGS[0]['share_url'], unescape(page))
        self.assertIn('.cfg-card .name{display:block;', page)
        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))', page)
        self.assertEqual(len(CONFIGS), 1)

    def test_empty_configs(self):
        page = render_subscription('Empty', [], 'example.test', '/i/token/sub')
        self.assertIn('No configurations available', page)
        self.assertIn('Configurations (0)', page)

    def test_formats(self):
        asyncio.run(self._formats())

    async def _formats(self):
        app = FastAPI()
        app.include_router(gateway.router)
        pool = AsyncMock()
        pool.fetchrow.return_value = {'name': 'Example', 'public_host': 'example.test', 'status': 'running'}
        target = {'instance_id': 'example', 'worker_url': 'http://worker', 'upstream': '/proxy'}
        response = httpx.Response(200, json={'links': CONFIGS}, request=httpx.Request('POST', 'http://worker/share'))
        upstream = AsyncMock()
        upstream.__aenter__.return_value = upstream
        upstream.post.return_value = response
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
            with patch.object(gateway, '_resolve_endpoint', AsyncMock(return_value=target)), patch.object(gateway, 'get_pool', return_value=pool), patch('httpx.AsyncClient', return_value=upstream):
                browser = {'user-agent': 'Mozilla/5.0', 'accept': 'text/html'}
                for headers in ({}, {'user-agent': 'Mozilla/5.0'}, {'user-agent': 'Happ', 'accept': 'text/html'}):
                    result = await client.get('/i/token/sub', headers=headers)
                    self.assertEqual(base64.b64decode(result.text).decode().splitlines(), [CONFIGS[0]['share_url']])
                    self.assertIn('subscription-userinfo', result.headers)
                result = await client.get('/i/token/sub', headers=browser)
                self.assertIn('text/html', result.headers['content-type'])
                self.assertIn('EMUNEL Panel', result.text)
                result = await client.get('/i/token/sub?fmt=singbox', headers=browser)
                self.assertEqual(result.json()['outbounds'][0]['type'], 'vless')
                self.assertNotIn('imArasTey', result.text)
                self.assertEqual(len(result.json()['outbounds']), 1)
                result = await client.get('/i/token/sub?fmt=clash', headers=browser)
                self.assertIn('proxies:', result.text)
                self.assertNotIn('<!DOCTYPE', result.text)


if __name__ == '__main__':
    unittest.main()
