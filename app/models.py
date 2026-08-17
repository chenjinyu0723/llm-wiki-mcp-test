"""API payloads. Runtime credentials are intentionally never persisted."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


PageType = Literal["concept", "entity", "query"]


class RuntimeConfig(BaseModel):
    public_mcp_url: str = ""
    admin_mcp_url: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    pass_model_to_mcp: bool = True

    @field_validator("public_mcp_url", "admin_mcp_url", "llm_base_url", "llm_api_key", "llm_model", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> str:
        return str(value or "").strip()


class KnowledgeBaseRequest(BaseModel):
    runtime: RuntimeConfig


class CreateKnowledgeBaseRequest(KnowledgeBaseRequest):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class DocumentsRequest(KnowledgeBaseRequest):
    knowledge_base_name: str = Field(min_length=1)


class DocumentMarkdownRequest(DocumentsRequest):
    filename: str = Field(min_length=1)
    max_chars: int = Field(default=30000, ge=1000, le=100000)


class CompileRequest(DocumentsRequest):
    filenames: list[str] = Field(default_factory=list, max_length=100)
    retry_failed: bool = False
    candidate_guidance: str = Field(default="", max_length=1000)
    max_candidates: int | None = Field(default=None, ge=1, le=50)


class JobRequest(KnowledgeBaseRequest):
    task_id: str = Field(min_length=1)


class DeleteDocumentsRequest(DocumentsRequest):
    filenames: list[str] = Field(min_length=1, max_length=100)


class WikiListRequest(KnowledgeBaseRequest):
    knowledge_base_names: list[str] = Field(default_factory=list, max_length=20)
    page_types: list[PageType] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)


class PageReference(BaseModel):
    knowledge_base_name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    page_type: PageType | None = None


class WikiReadRequest(KnowledgeBaseRequest):
    pages: list[PageReference] = Field(min_length=1, max_length=30)
    max_content_chars: int = Field(default=6000, ge=500, le=12000)
    include_tables: bool = False


class ConversationCreateRequest(KnowledgeBaseRequest):
    knowledge_base_names: list[str] = Field(default_factory=list, max_length=20)


class ConversationUpdateRequest(KnowledgeBaseRequest):
    conversation_id: str = Field(min_length=1)
    knowledge_base_names: list[str] = Field(default_factory=list, max_length=20)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ConversationRequest(KnowledgeBaseRequest):
    conversation_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)
    knowledge_base_names: list[str] = Field(default_factory=list, max_length=20)
    selected_pages: list[PageReference] = Field(default_factory=list, max_length=30)
    auto_retrieve: bool = True
    include_query_pages: bool = False
    include_tables: bool = False
    persist_question: bool = True


class RegenerateRequest(KnowledgeBaseRequest):
    conversation_id: str = Field(min_length=1)
    assistant_message_id: str = Field(min_length=1)
    include_query_pages: bool = False
    include_tables: bool = False
    persist_question: bool = True
