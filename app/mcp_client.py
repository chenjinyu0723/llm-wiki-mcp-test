"""A small Streamable HTTP MCP client used by the independent validator.

It intentionally calls only MCP tools, never the LLM Wiki REST API or its
database. Session IDs are pooled by endpoint and a hash of transient model
settings, so different browser configurations cannot share MCP sessions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import RuntimeConfig


class McpClientError(RuntimeError):
    """A transport, protocol, or tool-level MCP failure."""


@dataclass(frozen=True, slots=True)
class _SessionKey:
    url: str
    config_hash: str


class McpHttpClient:
    """Session-aware client for LLM Wiki's Streamable HTTP endpoints."""

    def __init__(self, *, timeout_seconds: float = 90, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), trust_env=False)
        self._owns_http_client = http_client is None
        self._sessions: dict[_SessionKey, str] = {}
        self._locks: dict[_SessionKey, asyncio.Lock] = {}

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    async def tool(
        self,
        url: str,
        name: str,
        arguments: dict[str, Any],
        runtime: RuntimeConfig,
    ) -> dict[str, Any]:
        """Call an MCP tool, re-initializing once when a session expires."""

        endpoint = self._endpoint(url)
        key = _SessionKey(endpoint, self._config_hash(runtime))
        for attempt in range(2):
            session_id = await self._session_id(key, runtime)
            response = await self._request(
                endpoint,
                self._headers(runtime, session_id),
                {
                    "jsonrpc": "2.0",
                    "id": uuid.uuid4().hex,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            if response.status_code == 404 and attempt == 0:
                self._sessions.pop(key, None)
                continue
            data = self._decode_response(response)
            result = data.get("result")
            if not isinstance(result, dict):
                raise McpClientError("MCP 工具未返回 result。")
            if result.get("isError"):
                detail = result.get("structuredContent") or result.get("content")
                raise McpClientError(f"MCP 工具《{name}》返回错误：{detail}")
            content = result.get("structuredContent")
            if not isinstance(content, dict):
                raise McpClientError(f"MCP 工具《{name}》缺少 structuredContent。")
            return content
        raise McpClientError("MCP 会话已过期，重新初始化后仍不可用。")

    async def release(self, url: str, runtime: RuntimeConfig) -> None:
        endpoint = self._endpoint(url)
        key = _SessionKey(endpoint, self._config_hash(runtime))
        session_id = self._sessions.pop(key, None)
        if not session_id:
            return
        try:
            await self._http.delete(endpoint, headers=self._headers(runtime, session_id))
        except httpx.HTTPError:
            pass

    async def _session_id(self, key: _SessionKey, runtime: RuntimeConfig) -> str:
        if session_id := self._sessions.get(key):
            return session_id
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if session_id := self._sessions.get(key):
                return session_id
            response = await self._request(
                key.url,
                self._headers(runtime),
                {
                    "jsonrpc": "2.0",
                    "id": uuid.uuid4().hex,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "llm-wiki-mcp-validator", "version": "0.1.0"},
                    },
                },
            )
            data = self._decode_response(response)
            if not isinstance(data.get("result"), dict):
                raise McpClientError("MCP initialize 失败。")
            session_id = response.headers.get("mcp-session-id")
            if not session_id:
                raise McpClientError("MCP 服务未返回 Mcp-Session-Id。")
            self._sessions[key] = session_id
            # This notification does not require a response. Failure is safe:
            # the LLM Wiki server accepts tools after initialize regardless.
            try:
                await self._http.post(
                    key.url,
                    headers=self._headers(runtime, session_id),
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                )
            except httpx.HTTPError:
                pass
            return session_id

    async def _request(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._http.post(url, headers=headers, json=payload)
        except httpx.HTTPError as error:
            raise McpClientError(f"MCP 网络请求失败：{error}") from error

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise McpClientError(f"MCP 返回非 JSON 响应（HTTP {response.status_code}）。") from error
        if not isinstance(data, dict):
            raise McpClientError("MCP 返回不是 JSON 对象。")
        if error := data.get("error"):
            message = error.get("message") if isinstance(error, dict) else error
            raise McpClientError(f"MCP 协议错误：{message}")
        if response.status_code >= 400:
            raise McpClientError(f"MCP HTTP 请求失败：{response.status_code}")
        return data

    @staticmethod
    def _endpoint(url: str) -> str:
        endpoint = url.strip().rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise McpClientError("MCP 地址必须以 http:// 或 https:// 开头。")
        if not endpoint.endswith("/mcp"):
            raise McpClientError("MCP 地址必须以 /mcp 结尾。")
        return endpoint

    @staticmethod
    def _headers(runtime: RuntimeConfig, session_id: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if runtime.pass_model_to_mcp:
            if runtime.llm_base_url:
                headers["X-LLM-Base-URL"] = runtime.llm_base_url
            if runtime.llm_api_key:
                headers["X-LLM-API-Key"] = runtime.llm_api_key
            if runtime.llm_model:
                headers["X-LLM-Model"] = runtime.llm_model
        return headers

    @staticmethod
    def _config_hash(runtime: RuntimeConfig) -> str:
        # Do not retain credentials as map keys or in log-friendly reprs.
        material = "\x1f".join(
            [
                "1" if runtime.pass_model_to_mcp else "0",
                runtime.llm_base_url if runtime.pass_model_to_mcp else "",
                runtime.llm_api_key if runtime.pass_model_to_mcp else "",
                runtime.llm_model if runtime.pass_model_to_mcp else "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
