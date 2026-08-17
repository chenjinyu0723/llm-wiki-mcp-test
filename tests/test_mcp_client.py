from __future__ import annotations

import json
import unittest

import httpx

from app.mcp_client import McpClientError, McpHttpClient
from app.models import RuntimeConfig


class McpHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_once_and_reuses_session(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            calls.append(payload["method"])
            if payload["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {}}, headers={"Mcp-Session-Id": "session-1"})
            if payload["method"] == "notifications/initialized":
                return httpx.Response(202)
            self.assertEqual(request.headers["Mcp-Session-Id"], "session-1")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"structuredContent": {"ok": True}, "isError": False}})

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = McpHttpClient(http_client=http_client)
        runtime = RuntimeConfig()
        self.assertEqual(await client.tool("http://test/mcp", "first", {}, runtime), {"ok": True})
        self.assertEqual(await client.tool("http://test/mcp", "second", {}, runtime), {"ok": True})
        self.assertEqual(calls.count("initialize"), 1)
        await http_client.aclose()

    async def test_surfaces_tool_error_without_parsing_text_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {}}, headers={"Mcp-Session-Id": "session-1"})
            if payload["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"structuredContent": {"error": "bad input"}, "isError": True}})

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = McpHttpClient(http_client=http_client)
        with self.assertRaisesRegex(McpClientError, "bad input"):
            await client.tool("http://test/mcp", "broken", {}, RuntimeConfig())
        await http_client.aclose()
