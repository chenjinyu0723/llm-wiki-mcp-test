"""Evidence-first multi-turn chat built entirely on top of public MCP."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.conversation_store import ConversationStore
from app.mcp_client import McpClientError, McpHttpClient
from app.models import ConversationRequest, RuntimeConfig

logger = logging.getLogger("validator.chat")


class ChatModelError(RuntimeError):
    pass


@dataclass(slots=True)
class PreparedTurn:
    conversation_id: str
    assistant_message_id: str
    question: str
    retrieval_query: str
    source_pages: list[dict[str, Any]]
    direct_pages: list[dict[str, Any]]
    warnings: list[str]
    messages: list[dict[str, str]]


class ChatService:
    def __init__(self, store: ConversationStore, mcp: McpHttpClient) -> None:
        self.store = store
        self.mcp = mcp
        self._active_streams: dict[tuple[str, str], asyncio.Task[Any]] = {}

    async def prepare(self, request: ConversationRequest, *, question: str | None = None, conversation_id: str | None = None, assistant_message_id: str | None = None) -> PreparedTurn:
        runtime = self._runtime(request.runtime)
        conversation_id = conversation_id or request.conversation_id
        if not conversation_id:
            created = self.store.create(request.knowledge_base_names)
            conversation_id = str(created["id"])
        conversation = self.store.get(conversation_id, required=True)
        clean_question = (question or request.question).strip()
        effective_request = request.model_copy(update={
            "knowledge_base_names": request.knowledge_base_names or conversation["knowledge_base_names"],
        })
        history = [item for item in conversation["messages"] if item["state"] == "completed"]
        retrieval_query = await self._rewrite_query(clean_question, conversation["memory_summary"], history, runtime)
        source_pages, direct_pages, warnings = await self._select_and_read(effective_request, retrieval_query, runtime)
        if assistant_message_id:
            # Regeneration already reset the same assistant slot.
            assistant_id = assistant_message_id
        else:
            _, assistant, _ = self.store.begin_turn(conversation_id, clean_question, effective_request.knowledge_base_names)
            assistant_id = str(assistant["id"])
        messages = self._answer_messages(clean_question, retrieval_query, conversation, history, source_pages, warnings)
        return PreparedTurn(conversation_id, assistant_id, clean_question, retrieval_query, source_pages, direct_pages, warnings, messages)

    async def stream_answer(self, prepared: PreparedTurn, runtime: RuntimeConfig) -> AsyncIterator[str]:
        async for delta in self._stream_model(prepared.messages, runtime):
            yield delta

    def register_stream(self, prepared: PreparedTurn) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_streams[(prepared.conversation_id, prepared.assistant_message_id)] = task

    def unregister_stream(self, prepared: PreparedTurn) -> None:
        key = (prepared.conversation_id, prepared.assistant_message_id)
        task = self._active_streams.get(key)
        if task is asyncio.current_task():
            self._active_streams.pop(key, None)

    def request_stop(self, conversation_id: str, assistant_message_id: str) -> bool:
        """Cancel the request task so the OpenAI stream closes instead of merely hiding it."""

        task = self._active_streams.get((conversation_id, assistant_message_id))
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def finalize(self, prepared: PreparedTurn, answer: str, runtime: RuntimeConfig, *, persist_question: bool) -> dict[str, Any]:
        knowledge_status, knowledge_message = "skipped", "本轮未执行问答沉淀。"
        if persist_question and prepared.direct_pages:
            evidence = [
                self._reference(page)
                for page in prepared.direct_pages
                if str(page.get("page_type")) in {"concept", "entity"}
            ]
            if evidence:
                try:
                    result = await self.mcp.tool(
                        self._runtime(runtime).public_mcp_url,
                        "persist_wiki_question",
                        {
                            "question": prepared.question,
                            "answer": answer,
                            "evidence_pages": evidence,
                            "knowledge_base_names": list(dict.fromkeys(item["knowledge_base_name"] for item in evidence)),
                        },
                        self._runtime(runtime),
                    )
                    knowledge_status = str(result.get("knowledge_status") or "skipped")
                    knowledge_message = str(result.get("knowledge_message") or "")
                    prepared.warnings.extend(str(item) for item in result.get("warnings", []) if item)
                except McpClientError as error:
                    prepared.warnings.append(f"问答沉淀失败，回答已保留：{error}")
                    knowledge_message = "沉淀服务暂时不可用，回答仍已完成。"
        message = self.store.complete(
            prepared.conversation_id,
            prepared.assistant_message_id,
            answer=answer,
            retrieval_query=prepared.retrieval_query,
            source_pages=prepared.source_pages,
            direct_evidence_pages=prepared.direct_pages,
            warnings=prepared.warnings,
            knowledge_status=knowledge_status,
            knowledge_message=knowledge_message,
        )
        if message is None:
            # A stop request may have won the race while persistence was running.
            cancelled_message = self.store.message(prepared.conversation_id, prepared.assistant_message_id, required=True)
            return {"message": cancelled_message, "knowledge_status": "skipped", "knowledge_message": "本轮已停止，未沉淀问答。", "warnings": prepared.warnings}
        await self._refresh_memory(prepared.conversation_id, runtime)
        return {"message": message, "knowledge_status": knowledge_status, "knowledge_message": knowledge_message, "warnings": prepared.warnings}

    def cancel(self, prepared: PreparedTurn, partial_answer: str) -> None:
        self.store.terminal(prepared.conversation_id, prepared.assistant_message_id, state="cancelled", content=partial_answer, error="用户停止了本轮回答。")

    def fail(self, prepared: PreparedTurn, partial_answer: str, error: str) -> None:
        self.store.terminal(prepared.conversation_id, prepared.assistant_message_id, state="failed", content=partial_answer, error=error)

    async def _select_and_read(self, request: ConversationRequest, query: str, runtime: RuntimeConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        warnings: list[str] = []
        direct_pages: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        page_types = ["concept", "entity"] + (["query"] if request.include_query_pages else [])
        if request.selected_pages:
            direct_pages = [page.model_dump(mode="json") for page in request.selected_pages]
            pages = list(direct_pages)
        elif request.auto_retrieve:
            try:
                found = await self.mcp.tool(
                    runtime.public_mcp_url,
                    "find_wiki_pages",
                    {
                        "query": query,
                        "knowledge_base_names": request.knowledge_base_names,
                        "page_types": page_types,
                        "max_results": 12,
                        "include_two_hop_parents": True,
                    },
                    runtime,
                )
                pages = [item for item in found.get("pages", []) if isinstance(item, dict)]
                direct_pages = [item for item in pages if str(item.get("match")) == "direct_match"]
                warnings.extend(str(item) for item in found.get("warnings", []) if item)
            except McpClientError as error:
                warnings.append(f"Wiki 检索不可用，改为无知识库回答：{error}")
        if not pages:
            warnings.append("未找到相关 Wiki 页面，本轮将进行普通回答。")
            return [], [], list(dict.fromkeys(warnings))
        refs = [self._reference(page) for page in pages]
        try:
            read = await self.mcp.tool(
                runtime.public_mcp_url,
                "read_wiki_pages",
                {"pages": refs, "max_content_chars": 6000, "include_tables": request.include_tables},
                runtime,
            )
            read_pages = [item for item in read.get("pages", []) if isinstance(item, dict)]
            warnings.extend(str(item) for item in read.get("warnings", []) if item)
            resolved_direct_refs = {
                (str(item.get("knowledge_base_name")), str(item.get("title")), str(item.get("page_type") or ""))
                for item in direct_pages
            }
            resolved_direct_pages = [
                item for item in read_pages
                if (str(item.get("knowledge_base_name")), str(item.get("title")), str(item.get("page_type") or "")) in resolved_direct_refs
            ]
            return self._bound_pages(read_pages), resolved_direct_pages, list(dict.fromkeys(warnings))
        except McpClientError as error:
            warnings.append(f"Wiki 正文读取失败，改为无知识库回答：{error}")
            return [], direct_pages, list(dict.fromkeys(warnings))

    async def _rewrite_query(self, question: str, summary: str, history: list[dict[str, Any]], runtime: RuntimeConfig) -> str:
        if not summary.strip() and not history:
            return question
        recent = "\n".join(f"{'用户' if item['role'] == 'user' else '助手'}：{str(item['content'])[:400]}" for item in history[-settings.chat_history_messages:])
        messages = [
            {"role": "system", "content": "你是检索查询改写器。仅依据会话摘要和最近对话补足当前问题中的指代、省略和限定条件，把它改写成独立的中文 Wiki 检索问题；不得添加新事实。只输出改写后的问题，不要解释。"},
            {"role": "user", "content": f"会话摘要：{summary[:1200] or '（无）'}\n\n最近对话：\n{recent or '（无）'}\n\n当前问题：{question}"},
        ]
        try:
            result = await self._complete_model(messages, runtime, temperature=0.05)
            return result.strip()[:1600] or question
        except Exception as error:
            logger.info("validator.query_rewrite_fallback error=%s", error)
            return question

    def _answer_messages(self, question: str, retrieval_query: str, conversation: dict[str, Any], history: list[dict[str, Any]], pages: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, str]]:
        context = "\n\n".join(
            f"### {page.get('title')}\n摘要：{page.get('summary', '')}\n正文：{page.get('content_markdown', '')}\n表格：{self._tables_text(page.get('tables'))}"
            for page in pages
        )
        recent = "\n".join(f"{'用户' if item['role'] == 'user' else '助手'}：{str(item['content'])[:800]}" for item in history[-settings.chat_history_messages:])
        return [
            {"role": "system", "content": "你是严谨的中文知识问答助手。Wiki 正文和表格是事实证据；只能据此回答具体事实，证据不足时明确说明。会话摘要和历史仅用于理解指代，不是事实来源。若没有 Wiki 证据，仍正常回答，但不要编造看似来自知识库的内容。回答正文不要出现“参考某 Wiki”“来自知识库”等来源归因，不要列页面标题；来源由界面在回答下方展示。使用清晰 Markdown。"},
            {"role": "user", "content": f"当前问题：{question}\n\n检索问题：{retrieval_query}\n\n会话摘要：{str(conversation.get('memory_summary') or '')[:1200] or '（无）'}\n\n最近对话：{recent or '（无）'}\n\nWiki 证据：{context or '（未找到相关 Wiki 页面）'}\n\n检索提示：{'；'.join(warnings) or '无'}"},
        ]

    async def _stream_model(self, messages: list[dict[str, str]], runtime: RuntimeConfig, *, temperature: float = 0.2) -> AsyncIterator[str]:
        base_url, api_key, model = runtime.llm_base_url, runtime.llm_api_key, runtime.llm_model
        if not all((base_url, api_key, model)):
            raise ChatModelError("请在验证项目配置中填写对话模型 Base URL、API Key 和模型名。")
        self._configure_network()
        last_error: Exception | None = None
        for attempt in range(settings.chat_max_retries + 1):
            http_client = httpx.AsyncClient(
                verify=False,
                timeout=httpx.Timeout(settings.chat_timeout_seconds),
                transport=httpx.AsyncHTTPTransport(verify=False),
                trust_env=False,
            )
            client = AsyncOpenAI(api_key=api_key, base_url=self._base_url(base_url), http_client=http_client)
            yielded = False
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    stream=True,
                    extra_body={"chat_template_kwargs": {"enable_thinking": True}, "thinking": {"type": "enabled"}},
                )
                async for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yielded = True
                        yield chunk.choices[0].delta.content
                if yielded:
                    return
                raise ChatModelError("对话模型未返回可展示的正文。")
            except asyncio.CancelledError:
                raise
            except (ChatModelError, APITimeoutError, APIConnectionError, APIStatusError, httpx.HTTPError) as error:
                last_error = error
                retryable = not isinstance(error, APIStatusError) or error.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
                if yielded or not retryable or attempt >= settings.chat_max_retries:
                    raise ChatModelError(f"对话模型调用失败：{error}") from error
                delay = min(4.0, 0.5 * (2 ** attempt))
                logger.info("validator.chat_retry attempt=%s delay=%s error=%s", attempt + 1, delay, error)
                await asyncio.sleep(delay)
            finally:
                await client.close()
                await http_client.aclose()
        raise ChatModelError(f"对话模型调用失败：{last_error or '未知错误'}")

    async def _complete_model(self, messages: list[dict[str, str]], runtime: RuntimeConfig, *, temperature: float) -> str:
        chunks: list[str] = []
        async for chunk in self._stream_model(messages, runtime, temperature=temperature):
            chunks.append(chunk)
        return "".join(chunks)

    async def _refresh_memory(self, conversation_id: str, runtime: RuntimeConfig) -> None:
        conversation = self.store.get(conversation_id, required=True)
        messages = [item for item in conversation["messages"] if item["state"] == "completed"]
        if len(messages) <= settings.chat_history_messages:
            return
        cutoff = messages[-settings.chat_history_messages - 1]
        through = int(conversation["summary_through_sequence"])
        older = [item for item in messages if through < int(item["sequence"]) <= int(cutoff["sequence"])]
        if not older:
            return
        dialogue = "\n".join(f"{'用户' if item['role'] == 'user' else '助手'}：{str(item['content'])[:600]}" for item in older)
        try:
            summary = await self._complete_model(
                [
                    {"role": "system", "content": "你是对话记忆压缩器。保留用户目标、关键限制和未完成事项，压缩成不超过 800 字中文摘要；不要保存 Wiki 标题、来源、链接或未经证实的事实。只输出摘要。"},
                    {"role": "user", "content": f"已有摘要：{conversation['memory_summary'][:1200] or '（无）'}\n\n较早对话：\n{dialogue}"},
                ],
                runtime,
                temperature=0.05,
            )
            if summary.strip():
                self.store.update_memory(conversation_id, summary.strip()[:1200], int(cutoff["sequence"]))
        except Exception as error:
            logger.info("validator.memory_summary_skipped error=%s", error)

    def _runtime(self, runtime: RuntimeConfig) -> RuntimeConfig:
        return runtime.model_copy(update={
            "public_mcp_url": runtime.public_mcp_url or settings.public_mcp_url,
            "admin_mcp_url": runtime.admin_mcp_url or settings.admin_mcp_url,
        })

    @staticmethod
    def _reference(page: dict[str, Any]) -> dict[str, str]:
        return {key: str(page[key]) for key in ("knowledge_base_name", "title", "page_type") if page.get(key)}

    @staticmethod
    def _tables_text(value: object) -> str:
        if not isinstance(value, list):
            return "（无）"
        return "\n".join(f"{item.get('caption', '表格')}\n{item.get('content_markdown', '')}" for item in value if isinstance(item, dict)) or "（无）"

    @staticmethod
    def _bound_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        total = 0
        for page in pages:
            content = str(page.get("content_markdown") or "")
            if total >= settings.chat_context_chars:
                break
            page = dict(page)
            page["content_markdown"] = content[: max(500, settings.chat_context_chars - total)]
            result.append(page)
            total += len(page["content_markdown"])
        return result

    @staticmethod
    def _base_url(url: str) -> str:
        normalized = url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            normalized = normalized[: -len("/chat/completions")]
        return normalized + "/" if normalized.endswith("/v1") else normalized

    @staticmethod
    def _configure_network() -> None:
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
