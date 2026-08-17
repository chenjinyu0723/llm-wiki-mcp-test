"""FastAPI BFF and local SPA for validating both LLM Wiki MCP services."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.chat_service import ChatModelError, ChatService
from app.config import settings
from app.conversation_store import ConversationStore
from app.mcp_client import McpClientError, McpHttpClient
from app.models import (
    CompileRequest,
    ConversationCreateRequest,
    ConversationRequest,
    ConversationUpdateRequest,
    CreateKnowledgeBaseRequest,
    DocumentMarkdownRequest,
    DeleteDocumentsRequest,
    DocumentsRequest,
    JobRequest,
    KnowledgeBaseRequest,
    RegenerateRequest,
    RuntimeConfig,
    WikiListRequest,
    WikiReadRequest,
)

logger = logging.getLogger("validator")
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
store = ConversationStore(settings.database_path)
mcp = McpHttpClient()
chat = ChatService(store, mcp)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    store.initialize()
    yield
    await mcp.close()


app = FastAPI(title="LLM Wiki MCP 验证台", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "llm-wiki-mcp-validator"}


@app.post("/api/connections/test")
async def test_connections(payload: KnowledgeBaseRequest) -> dict[str, Any]:
    """Check both separate MCP processes, without requiring a model call."""

    runtime = _runtime(payload.runtime)
    public, admin = await asyncio.gather(
        _public(runtime, "list_knowledge_bases", {}),
        _admin(runtime, "list_knowledge_bases", {}),
    )
    return {
        "public_knowledge_base_count": len(public.get("knowledge_bases", [])),
        "admin_knowledge_base_count": len(admin.get("knowledge_bases", [])),
        "message": "公开 MCP 与管理员 MCP 均可用。",
    }


@app.post("/api/manage/bases")
async def list_bases(payload: KnowledgeBaseRequest) -> dict[str, Any]:
    return await _admin(payload.runtime, "list_knowledge_bases", {})


@app.post("/api/manage/bases/create")
async def create_base(payload: CreateKnowledgeBaseRequest) -> dict[str, Any]:
    return await _admin(payload.runtime, "create_knowledge_base", {"name": payload.name, "description": payload.description})


@app.post("/api/manage/documents")
async def list_documents(payload: DocumentsRequest) -> dict[str, Any]:
    return await _admin(payload.runtime, "list_documents", {"knowledge_base_names": [payload.knowledge_base_name], "include_assets": True})


@app.post("/api/manage/documents/markdown")
async def document_markdown(payload: DocumentMarkdownRequest) -> dict[str, Any]:
    return await _admin(payload.runtime, "read_document_markdown", {"knowledge_base_name": payload.knowledge_base_name, "filename": payload.filename, "max_chars": payload.max_chars})


@app.post("/api/manage/documents/upload")
async def upload_documents(
    knowledge_base_name: str = Form(...),
    runtime_json: str = Form(...),
    compile_enabled: bool = Form(True),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    runtime = _runtime_json(runtime_json)
    if len(files) > settings.max_upload_files:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {settings.max_upload_files} 个文件。")
    batch_dir = settings.upload_dir / uuid.uuid4().hex
    batch_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    failed: list[dict[str, str]] = []
    seen_filenames: set[str] = set()
    try:
        for upload in files:
            try:
                filename = Path(upload.filename or "").name
                if not filename or filename != (upload.filename or ""):
                    failed.append({"filename": upload.filename or "", "error": "文件名不合法。"})
                    continue
                if filename.casefold() in seen_filenames:
                    failed.append({"filename": filename, "error": "同一批次不能上传重名文件。"})
                    continue
                seen_filenames.add(filename.casefold())
                target = batch_dir / filename
                size = 0
                with target.open("wb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            raise ValueError(f"单个文件不能超过 {settings.max_upload_bytes // 1024 // 1024} MB。")
                        output.write(chunk)
                paths.append(target)
            except (OSError, ValueError) as error:
                failed.append({"filename": upload.filename or "", "error": str(error)})
            finally:
                await upload.close()
        if paths:
            imported = await _admin(runtime, "import_documents", {"knowledge_base_name": knowledge_base_name, "source_paths": [str(path.resolve()) for path in paths], "compile_enabled": compile_enabled})
        else:
            imported = {"imported": [], "failed": []}
        imported.setdefault("failed", []).extend(failed)
        return imported
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)


@app.post("/api/manage/compile")
async def compile_documents(payload: CompileRequest) -> dict[str, Any]:
    arguments = {
        "knowledge_base_name": payload.knowledge_base_name,
        "filenames": payload.filenames,
        "retry_failed": payload.retry_failed,
        "candidate_guidance": payload.candidate_guidance,
    }
    if payload.max_candidates is not None:
        arguments["max_candidates"] = payload.max_candidates
    return await _admin(payload.runtime, "compile_documents", arguments)


@app.post("/api/manage/jobs/status")
async def job_status(payload: JobRequest) -> dict[str, Any]:
    return await _admin(payload.runtime, "get_job_status", {"task_id": payload.task_id, "event_limit": 100})


@app.post("/api/manage/documents/delete")
async def delete_documents(payload: DeleteDocumentsRequest) -> dict[str, Any]:
    return await _admin(payload.runtime, "delete_documents", {"knowledge_base_name": payload.knowledge_base_name, "filenames": payload.filenames})


@app.post("/api/manage/wiki")
async def list_wiki(payload: WikiListRequest) -> dict[str, Any]:
    return await _public(payload.runtime, "list_wiki_pages", {
        "knowledge_base_names": payload.knowledge_base_names,
        "page_types": payload.page_types,
        "limit": payload.limit,
        "offset": payload.offset,
    })


@app.post("/api/manage/wiki/read")
async def read_wiki(payload: WikiReadRequest) -> dict[str, Any]:
    return await _public(payload.runtime, "read_wiki_pages", {
        "pages": [item.model_dump(exclude_none=True) for item in payload.pages],
        "max_content_chars": payload.max_content_chars,
        "include_tables": payload.include_tables,
    })


@app.post("/api/chat/conversations")
async def create_conversation(payload: ConversationCreateRequest) -> dict[str, Any]:
    return store.create(payload.knowledge_base_names)


@app.get("/api/chat/conversations")
async def get_conversations() -> list[dict[str, Any]]:
    return store.list()


@app.get("/api/chat/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    item = store.get(conversation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="对话不存在。")
    return item


@app.put("/api/chat/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, payload: ConversationUpdateRequest) -> dict[str, Any]:
    if conversation_id != payload.conversation_id:
        raise HTTPException(status_code=400, detail="对话 ID 不一致。")
    return store.update(conversation_id, title=payload.title, knowledge_base_names=payload.knowledge_base_names)


@app.delete("/api/chat/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, bool]:
    return {"deleted": store.delete(conversation_id)}


@app.post("/api/chat/stream")
async def chat_stream(payload: ConversationRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(_chat_events(payload, request), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/regenerate")
async def regenerate(payload: RegenerateRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(_regenerate_events(payload, request), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/stop")
async def stop_chat(payload: RegenerateRequest) -> dict[str, str]:
    message = store.message(payload.conversation_id, payload.assistant_message_id)
    if message and message["state"] == "streaming":
        store.terminal(payload.conversation_id, payload.assistant_message_id, state="cancelled", content=message["content"], error="用户停止了本轮回答。")
        model_cancel_requested = chat.request_stop(payload.conversation_id, payload.assistant_message_id)
        return {"state": "cancelled", "model_cancel_requested": str(model_cancel_requested).lower()}
    return {"state": str(message["state"] if message else "missing"), "model_cancel_requested": "false"}


async def _chat_events(payload: ConversationRequest, request: Request):
    prepared = None
    answer_parts: list[str] = []
    try:
        prepared = await chat.prepare(payload)
        chat.register_stream(prepared)
        yield _event("meta", {"conversation_id": prepared.conversation_id, "assistant_message_id": prepared.assistant_message_id, "retrieval_query": prepared.retrieval_query, "pages": prepared.source_pages, "direct_pages": prepared.direct_pages, "warnings": prepared.warnings})
        async for delta in chat.stream_answer(prepared, payload.runtime):
            if await request.is_disconnected():
                raise asyncio.CancelledError
            answer_parts.append(delta)
            yield _event("delta", {"text": delta})
        result = await chat.finalize(prepared, "".join(answer_parts).strip(), payload.runtime, persist_question=payload.persist_question)
        yield _event("completed", result)
    except asyncio.CancelledError:
        if prepared is not None:
            chat.cancel(prepared, "".join(answer_parts))
        return
    except Exception as error:
        if prepared is not None:
            chat.fail(prepared, "".join(answer_parts), str(error))
        yield _event("error", {"message": str(error)})
    finally:
        if prepared is not None:
            chat.unregister_stream(prepared)


async def _regenerate_events(payload: RegenerateRequest, request: Request):
    prepared = None
    answer_parts: list[str] = []
    try:
        conversation, assistant, _, question = store.reset_for_regeneration(payload.conversation_id, payload.assistant_message_id)
        base_request = ConversationRequest(
            runtime=payload.runtime,
            conversation_id=payload.conversation_id,
            question=question,
            knowledge_base_names=conversation["knowledge_base_names"],
            include_query_pages=payload.include_query_pages,
            include_tables=payload.include_tables,
            persist_question=payload.persist_question,
        )
        prepared = await chat.prepare(base_request, question=question, conversation_id=payload.conversation_id, assistant_message_id=str(assistant["id"]))
        chat.register_stream(prepared)
        yield _event("meta", {"conversation_id": prepared.conversation_id, "assistant_message_id": prepared.assistant_message_id, "retrieval_query": prepared.retrieval_query, "pages": prepared.source_pages, "direct_pages": prepared.direct_pages, "warnings": prepared.warnings})
        async for delta in chat.stream_answer(prepared, payload.runtime):
            if await request.is_disconnected():
                raise asyncio.CancelledError
            answer_parts.append(delta)
            yield _event("delta", {"text": delta})
        result = await chat.finalize(prepared, "".join(answer_parts).strip(), payload.runtime, persist_question=payload.persist_question)
        yield _event("completed", result)
    except asyncio.CancelledError:
        if prepared is not None:
            chat.cancel(prepared, "".join(answer_parts))
        return
    except Exception as error:
        if prepared is not None:
            chat.fail(prepared, "".join(answer_parts), str(error))
        yield _event("error", {"message": str(error)})
    finally:
        if prepared is not None:
            chat.unregister_stream(prepared)


async def _public(runtime: RuntimeConfig, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime(runtime)
    try:
        return await mcp.tool(runtime.public_mcp_url, tool, arguments, runtime)
    except McpClientError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


async def _admin(runtime: RuntimeConfig, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime(runtime)
    try:
        return await mcp.tool(runtime.admin_mcp_url, tool, arguments, runtime)
    except McpClientError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


def _runtime(runtime: RuntimeConfig) -> RuntimeConfig:
    return runtime.model_copy(update={"public_mcp_url": runtime.public_mcp_url or settings.public_mcp_url, "admin_mcp_url": runtime.admin_mcp_url or settings.admin_mcp_url})


def _runtime_json(value: str) -> RuntimeConfig:
    try:
        return RuntimeConfig.model_validate_json(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"runtime 配置不是有效 JSON：{error}") from error


def _event(name: str, data: object) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
