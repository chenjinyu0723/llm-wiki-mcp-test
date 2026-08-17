from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, AsyncIterator

from app.chat_service import ChatService
from app.conversation_store import ConversationStore
from app.models import ConversationRequest, RuntimeConfig


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def tool(self, _: str, name: str, arguments: dict[str, Any], __: RuntimeConfig) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "find_wiki_pages":
            return {"pages": [
                {"knowledge_base_name": "测试库", "title": "直接页", "page_type": "concept", "summary": "直接证据", "match": "direct_match"},
                {"knowledge_base_name": "测试库", "title": "上级页", "page_type": "concept", "summary": "背景", "match": "parent_context"},
            ], "warnings": []}
        if name == "read_wiki_pages":
            return {"pages": [
                {"knowledge_base_name": "测试库", "title": "直接页", "page_type": "concept", "summary": "直接证据", "content_markdown": "直接正文", "match": "requested"},
                {"knowledge_base_name": "测试库", "title": "上级页", "page_type": "concept", "summary": "背景", "content_markdown": "上级正文", "match": "requested"},
            ], "warnings": []}
        if name == "persist_wiki_question":
            return {"knowledge_status": "created", "knowledge_message": "已沉淀", "warnings": []}
        raise AssertionError(f"unexpected tool: {name}")


class StubChatService(ChatService):
    async def _rewrite_query(self, question: str, *_: object) -> str:
        return question

    async def _stream_model(self, _: list[dict[str, str]], __: RuntimeConfig, *, temperature: float = 0.2) -> AsyncIterator[str]:
        del temperature
        yield "这是回答。"


class ChatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.temp.name) / "conversation.db")
        self.store.initialize()
        self.mcp = FakeMcp()
        self.service = StubChatService(self.store, self.mcp)  # type: ignore[arg-type]
        self.runtime = RuntimeConfig(llm_base_url="http://model/v1", llm_api_key="key", llm_model="model")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_uses_direct_pages_only_when_persisting_question(self) -> None:
        prepared = await self.service.prepare(ConversationRequest(runtime=self.runtime, question="这是什么？", knowledge_base_names=["测试库"]))
        self.assertEqual([item["title"] for item in prepared.direct_pages], ["直接页"])
        result = await self.service.finalize(prepared, "这是回答。", self.runtime, persist_question=True)
        self.assertEqual(result["knowledge_status"], "created")
        persist = next(arguments for name, arguments in self.mcp.calls if name == "persist_wiki_question")
        self.assertEqual([item["title"] for item in persist["evidence_pages"]], ["直接页"])

    async def test_regeneration_scope_can_fall_back_to_conversation_scope(self) -> None:
        conversation = self.store.create(["测试库"])
        prepared = await self.service.prepare(ConversationRequest(runtime=self.runtime, conversation_id=conversation["id"], question="第一个问题"))
        self.assertEqual(prepared.direct_pages[0]["knowledge_base_name"], "测试库")

    async def test_cancelled_message_cannot_be_overwritten_by_completion(self) -> None:
        conversation = self.store.create(["测试库"])
        _, assistant, _ = self.store.begin_turn(conversation["id"], "问题", ["测试库"])
        self.store.terminal(conversation["id"], assistant["id"], state="cancelled", content="部分回答", error="用户停止")
        complete = self.store.complete(
            conversation["id"], assistant["id"], answer="完整回答", retrieval_query="问题", source_pages=[], direct_evidence_pages=[],
            warnings=[], knowledge_status="skipped", knowledge_message="",
        )
        self.assertIsNone(complete)
        self.assertEqual(self.store.message(conversation["id"], assistant["id"], required=True)["state"], "cancelled")

    def test_normalizes_complete_openai_chat_endpoint(self) -> None:
        self.assertEqual(self.service._base_url("https://example.com/v1/chat/completions"), "https://example.com/v1/")
