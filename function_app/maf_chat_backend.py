from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_framework.azure import AzureAIProjectAgentProvider
from azure.ai.projects import AIProjectClient
from azure.cosmos import CosmosClient, PartitionKey, exceptions
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from dotenv import load_dotenv
from fastmcp import Client, FastMCP
from fastmcp.tools.tool import ToolResult
from openai import AzureOpenAI
from pydantic import BaseModel, Field

load_dotenv(override=False)


@dataclass(frozen=True)
class Settings:
    history_provider: str = os.getenv("HISTORY_PROVIDER", "local").strip().lower()
    max_recent_turns: int = int(os.getenv("MAX_RECENT_TURNS", "6"))
    summarize_after_pairs: int = int(
        os.getenv(
            "SUMMARIZE_AFTER_PAIRS",
            str(max(1, int(os.getenv("SUMMARIZE_AFTER_TURNS", "12")) // 2)),
        )
    )
    retrieval_top_k: int = int(os.getenv("RETRIEVAL_TOP_K", "5"))
    mcp_server_url: str = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8100/mcp")
    local_history_path: str = os.getenv("LOCAL_HISTORY_PATH", "./data/history")
    azure_search_endpoint: str = os.getenv("AZURE_SEARCH_ENDPOINT", "")
    azure_search_index: str = os.getenv("AZURE_SEARCH_INDEX", "")
    azure_search_semantic_configuration: str = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG", "")
    foundry_chat_endpoint: str = os.getenv("FOUNDRY_CHAT_ENDPOINT", "")
    foundry_api_key: str = os.getenv("FOUNDRY_API_KEY", "")
    foundry_chat_deployment: str = os.getenv("FOUNDRY_CHAT_DEPLOYMENT", "")
    foundry_api_version: str = os.getenv("FOUNDRY_API_VERSION", "2024-10-21")
    cosmos_endpoint: str = os.getenv("COSMOS_ENDPOINT", "")
    cosmos_key: str = os.getenv("COSMOS_KEY", "")
    cosmos_database: str = os.getenv("COSMOS_DATABASE", "chat")
    cosmos_container: str = os.getenv("COSMOS_CONTAINER", "sessions")
    cosmos_partition_key_path: str = os.getenv("COSMOS_PARTITION_KEY_PATH", "/sessionid")


settings = Settings()

_otel_configured = False


def setup_logging() -> None:
    global _otel_configured

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)

    if _otel_configured:
        return

    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "maf-chat-function-app").strip() or "maf-chat-function-app"
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor  # type: ignore[import-not-found]

        configure_azure_monitor(
            connection_string=connection_string,
            logger_name="",
            resource={"service.name": service_name},
        )
        _otel_configured = True
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to enable Azure Monitor OpenTelemetry: %s", str(exc))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Turn:
    role: str
    content: str
    timestamp: str = field(default_factory=utc_now_iso)


@dataclass
class Citation:
    chunk_id: str
    source: str
    title: str
    score: float


@dataclass
class RetrievalChunk:
    chunk_id: str
    content: str
    citation: Citation
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    chunks: list[RetrievalChunk]
    query_text: str
    top_k: int


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    user_query: str = Field(..., min_length=1, max_length=4096)


class ChatHttpResponse(BaseModel):
    session_id: str
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    grounded: bool = False
    trace: dict[str, Any] = Field(default_factory=dict)
    evaluator: dict[str, Any] = Field(default_factory=dict)


class JsonHistoryProvider:
    def __init__(self, root_path: str) -> None:
        self._root = Path(root_path)
        self._root.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _default_payload(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "turns": [],
            "summary": "",
            "summary_metadata": {
                "covered_turn_count": 0,
                "updated_at": "",
                "version": 1,
            },
        }

    def session_exists(self, session_id: str) -> bool:
        return self._session_path(session_id).exists()

    def load_session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if not path.exists():
            return self._default_payload(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.rename(path.with_suffix(".corrupted.json"))
            return self._default_payload(session_id)

        payload.setdefault("turns", [])
        payload.setdefault("summary", "")
        payload.setdefault(
            "summary_metadata",
            {"covered_turn_count": 0, "updated_at": "", "version": 1},
        )
        payload["session_id"] = str(payload.get("session_id") or session_id)
        return payload

    def save_session(self, session_id: str, payload: dict[str, Any]) -> None:
        path = self._session_path(session_id)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


class McpCosmosHistoryProvider:
    def __init__(self, mcp: "MCPServer") -> None:
        self._mcp = mcp
        partition_key_path = settings.cosmos_partition_key_path.strip() or "/sessionid"
        if not partition_key_path.startswith("/"):
            partition_key_path = f"/{partition_key_path}"
        self._partition_key_field = partition_key_path.lstrip("/")

    def _default_payload(self, session_id: str) -> dict[str, Any]:
        payload = {
            "id": session_id,
            "session_id": session_id,
            "turns": [],
            "summary": "",
            "summary_metadata": {
                "covered_turn_count": 0,
                "updated_at": "",
                "version": 1,
            },
        }
        payload.setdefault(self._partition_key_field, session_id)
        return payload

    def session_exists(self, session_id: str) -> bool:
        result = self._mcp.call("cosmos.query", {"id": session_id})
        return bool(result.get("ok") and isinstance(result.get("data", {}).get("document"), dict))

    def load_session(self, session_id: str) -> dict[str, Any]:
        result = self._mcp.call("cosmos.query", {"id": session_id})
        document = result.get("data", {}).get("document") if isinstance(result, dict) else None
        if not isinstance(document, dict):
            return self._default_payload(session_id)

        document.setdefault("turns", [])
        document.setdefault("summary", "")
        document.setdefault(
            "summary_metadata",
            {"covered_turn_count": 0, "updated_at": "", "version": 1},
        )
        document["id"] = str(document.get("id") or session_id)
        document["session_id"] = str(document.get("session_id") or session_id)
        document[self._partition_key_field] = str(document.get(self._partition_key_field) or session_id)
        return document

    def save_session(self, session_id: str, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized["id"] = str(normalized.get("id") or session_id)
        normalized["session_id"] = str(normalized.get("session_id") or session_id)
        normalized[self._partition_key_field] = str(normalized.get(self._partition_key_field) or session_id)
        result = self._mcp.call("cosmos.upsert", {"document": normalized})
        if not result.get("ok"):
            raise RuntimeError(str(result.get("details") or result.get("error") or "cosmos upsert failed"))


class SessionManager:
    def __init__(
        self,
        provider: JsonHistoryProvider | McpCosmosHistoryProvider,
        summarize_fn: Callable[[str, str], str] | None = None,
    ) -> None:
        self._provider = provider
        self._summarize_fn = summarize_fn

    def ensure_session(self, session_id: str) -> str:
        if self._provider.session_exists(session_id):
            return "reused"
        self._provider.save_session(session_id, self._provider.load_session(session_id))
        return "created"

    def _covered_turn_count(self, payload: dict[str, Any], turn_count: int) -> int:
        metadata = payload.get("summary_metadata")
        if not isinstance(metadata, dict):
            return 0
        try:
            return max(0, min(int(metadata.get("covered_turn_count", 0)), turn_count))
        except (TypeError, ValueError):
            return 0

    def get_context(self, session_id: str) -> dict[str, Any]:
        payload = self._provider.load_session(session_id)
        turns = [Turn(**turn) for turn in payload.get("turns", []) if isinstance(turn, dict)]
        covered_turn_count = self._covered_turn_count(payload, len(turns))
        recent = turns[covered_turn_count:][-settings.max_recent_turns :]
        return {
            "session_id": session_id,
            "recent_window": [turn.__dict__ for turn in recent],
            "summary": str(payload.get("summary") or ""),
        }

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        payload = self._provider.load_session(session_id)
        payload.setdefault("turns", []).append(Turn(role=role, content=content).__dict__)
        self._maybe_refresh_summary(payload, appended_role=role)
        self._provider.save_session(session_id, payload)

    def _maybe_refresh_summary(self, payload: dict[str, Any], *, appended_role: str) -> None:
        if self._summarize_fn is None or appended_role != "assistant":
            return

        turns = [Turn(**turn) for turn in payload.get("turns", []) if isinstance(turn, dict)]
        covered_turn_count = self._covered_turn_count(payload, len(turns))
        unsummarized_turns = turns[covered_turn_count:]
        user_count = sum(1 for turn in unsummarized_turns if turn.role == "user")
        assistant_count = sum(1 for turn in unsummarized_turns if turn.role == "assistant")
        if min(user_count, assistant_count) < settings.summarize_after_pairs:
            return

        summarize_count = len(unsummarized_turns) - settings.max_recent_turns
        if summarize_count < 2:
            return

        turns_to_summarize = unsummarized_turns[:summarize_count]
        if len(turns_to_summarize) % 2 != 0:
            turns_to_summarize = turns_to_summarize[:-1]
        if not turns_to_summarize:
            return

        previous_summary = str(payload.get("summary") or "")
        summary_segment = "\n".join(f"{turn.role}: {turn.content}" for turn in turns_to_summarize)
        payload["summary"] = self._summarize_fn(previous_summary, summary_segment)
        metadata = payload.setdefault(
            "summary_metadata",
            {"covered_turn_count": 0, "updated_at": "", "version": 1},
        )
        metadata["covered_turn_count"] = covered_turn_count + len(turns_to_summarize)
        metadata["updated_at"] = utc_now_iso()
        metadata["version"] = 1


class FoundryClient:
    def _client(self) -> AzureOpenAI:
        if not (settings.foundry_chat_endpoint and settings.foundry_chat_deployment):
            raise ValueError(
                "Foundry client is not configured. Set FOUNDRY_CHAT_ENDPOINT and FOUNDRY_CHAT_DEPLOYMENT."
            )
        if settings.foundry_api_key:
            return AzureOpenAI(
                azure_endpoint=settings.foundry_chat_endpoint,
                api_key=settings.foundry_api_key,
                azure_deployment=settings.foundry_chat_deployment,
                api_version=settings.foundry_api_version,
            )

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        return AzureOpenAI(
            azure_endpoint=settings.foundry_chat_endpoint,
            azure_ad_token_provider=token_provider,
            azure_deployment=settings.foundry_chat_deployment,
            api_version=settings.foundry_api_version,
        )

    def summarize_history(self, previous_summary: str, new_history_segment: str) -> str:
        response = self._client().chat.completions.create(
            model=settings.foundry_chat_deployment,
            messages=[
                {
                    "role": "system",
                    "content": "Summarize only the explicit content. Return strict JSON with key summary.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Previous summary:\n{previous_summary or '(none)'}\n\n"
                        f"Newly aged-out history segment:\n{new_history_segment}"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=500,
        )
        raw = (response.choices[0].message.content or "{}").strip()
        try:
            return str(json.loads(raw).get("summary") or raw).strip()
        except json.JSONDecodeError:
            return raw


class AzureSearchClient:
    def __init__(self) -> None:
        if not (settings.azure_search_endpoint and settings.azure_search_index):
            raise ValueError(
                "Azure Search client is not configured. Set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX."
            )
        self._client = SearchClient(
            endpoint=settings.azure_search_endpoint,
            index_name=settings.azure_search_index,
            credential=DefaultAzureCredential(),
        )

    def _build_filter(self, filters: dict[str, Any] | None) -> str | None:
        if not isinstance(filters, dict) or not filters:
            return None

        clauses: list[str] = []
        for key, value in filters.items():
            if isinstance(value, str):
                safe_value = value.replace("'", "''")
                clauses.append(f"{key} eq '{safe_value}'")
            elif isinstance(value, bool):
                clauses.append(f"{key} eq {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                clauses.append(f"{key} eq {value}")
        return " and ".join(clauses) if clauses else None

    def _serialize_caption(self, caption: Any) -> dict[str, Any]:
        return {
            "text": str(getattr(caption, "text", "") or ""),
            "highlights": str(getattr(caption, "highlights", "") or ""),
        }

    def _to_jsonable(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [self._to_jsonable(item) for item in value]
        if isinstance(value, tuple):
            return [self._to_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._to_jsonable(item) for key, item in value.items()}
        return str(value)

    def _map_result(self, result: dict[str, Any], original_filters: dict[str, Any] | None) -> RetrievalChunk:
        chunk_id = str(result.get("chunk_id") or result.get("id") or "")
        content = str(result.get("description_data") or "")
        source = str(result.get("source") or "")
        title = str(result.get("filename") or "")
        score = result.get("@search.reranker_score")
        if score is None:
            score = result.get("@search.score")

        metadata = dict(result)
        captions = metadata.get("@search.captions")
        if isinstance(captions, list):
            metadata["@search.captions"] = [self._serialize_caption(caption) for caption in captions]
        metadata = self._to_jsonable(metadata)
        metadata["filters"] = original_filters or {}
        return RetrievalChunk(
            chunk_id=chunk_id,
            content=content,
            citation=Citation(
                chunk_id=chunk_id,
                source=source,
                title=title,
                score=float(score or 0.0),
            ),
            metadata=metadata,
        )

    def search_hybrid(
        self,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
        select: list[str] | None = None,
    ) -> RetrievalResult:
        kwargs: dict[str, Any] = {"search_text": query, "top": top_k}
        filter_expr = self._build_filter(filters)
        if filter_expr:
            kwargs["filter"] = filter_expr
        if select:
            kwargs["select"] = select

        results = self._client.search(**kwargs)
        chunks = [self._map_result(dict(row), filters) for row in results]
        return RetrievalResult(chunks=chunks, query_text=query, top_k=top_k)


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class MCPServer:
    def __init__(self, name: str = "maf-chat-tools") -> None:
        self._mcp = FastMCP(name)

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        title: str | None = None,
    ) -> None:
        @self._mcp.tool(name=name, title=title)
        def wrapped_tool(payload: dict[str, Any]) -> ToolResult:
            return ToolResult(structured_content=handler(payload))

    def call(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = asyncio.run(self._mcp.call_tool(name, {"payload": payload}))
        except Exception as exc:
            return {"ok": False, "error": f"tool_call_failed:{name}", "details": str(exc)}
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        return {"ok": False, "error": f"tool_result_unhandled:{name}"}


def register_cosmos_tools(mcp: MCPServer) -> None:
    if not settings.cosmos_endpoint:
        def not_enabled(payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": False, "error": "cosmos_not_enabled", "details": payload.get("operation", "unknown")}

        mcp.register("cosmos.query", not_enabled, title="Cosmos Query")
        mcp.register("cosmos.upsert", not_enabled, title="Cosmos Upsert")
        return

    credential: str | DefaultAzureCredential = settings.cosmos_key or DefaultAzureCredential()
    partition_key_path = settings.cosmos_partition_key_path.strip() or "/session_id"
    if not partition_key_path.startswith("/"):
        partition_key_path = f"/{partition_key_path}"
    partition_key_field = partition_key_path.lstrip("/")
    client = CosmosClient(url=settings.cosmos_endpoint, credential=credential)
    database_client = client.create_database_if_not_exists(id=settings.cosmos_database)
    container_client = database_client.create_container_if_not_exists(
        id=settings.cosmos_container,
        partition_key=PartitionKey(path=partition_key_path),
    )

    def query(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("id") or payload.get("session_id") or "").strip()
        if not session_id:
            return {"ok": False, "error": "missing_id"}
        try:
            document = container_client.read_item(item=session_id, partition_key=session_id)
            return {"ok": True, "data": {"document": document}}
        except exceptions.CosmosResourceNotFoundError:
            return {"ok": True, "data": {"document": None}}
        except Exception as exc:
            return {"ok": False, "error": "cosmos_read_failed", "details": str(exc)}

    def upsert(payload: dict[str, Any]) -> dict[str, Any]:
        document = payload.get("document")
        if not isinstance(document, dict):
            return {"ok": False, "error": "missing_document"}
        session_id = str(document.get("session_id") or document.get("id") or "").strip()
        if not session_id:
            return {"ok": False, "error": "missing_session_id"}

        normalized = dict(document)
        normalized["id"] = str(normalized.get("id") or session_id)
        normalized["session_id"] = str(normalized.get("session_id") or session_id)
        normalized[partition_key_field] = str(normalized.get(partition_key_field) or session_id)
        try:
            stored = container_client.upsert_item(normalized)
        except Exception as exc:
            return {"ok": False, "error": "cosmos_upsert_failed", "details": str(exc)}
        return {"ok": True, "data": {"document": stored}}

    mcp.register("cosmos.query", query, title="Cosmos Query")
    mcp.register("cosmos.upsert", upsert, title="Cosmos Upsert")


def register_history_tools(mcp: MCPServer, session_manager: SessionManager) -> None:
    def ensure_session(payload: dict[str, Any]) -> dict[str, Any]:
        action = session_manager.ensure_session(payload["session_id"])
        return {"ok": True, "data": {"session_id": payload["session_id"], "action": action}}

    def get_context(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": session_manager.get_context(payload["session_id"])}

    def append_turn(payload: dict[str, Any]) -> dict[str, Any]:
        session_manager.append_turn(payload["session_id"], payload["role"], payload["content"])
        return {"ok": True}

    mcp.register("history.ensure_session", ensure_session, title="Ensure Session")
    mcp.register("history.get_context", get_context, title="Get Session Context")
    mcp.register("history.append_turn", append_turn, title="Append Chat Turn")


def register_retrieval_tools(mcp: MCPServer, client: AzureSearchClient) -> None:
    def hybrid_search(payload: dict[str, Any]) -> dict[str, Any]:
        result = client.search_hybrid(
            payload["query"],
            int(payload.get("top_k", 5)),
            payload.get("filters"),
            payload.get("select"),
        )
        return {
            "ok": True,
            "data": {
                "query_text": result.query_text,
                "top_k": result.top_k,
                "chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "content": chunk.content,
                        "citation": chunk.citation.__dict__,
                        "metadata": chunk.metadata,
                    }
                    for chunk in result.chunks
                ],
            },
        }

    mcp.register("retrieval.hybrid_search", hybrid_search, title="Hybrid Search Retrieval")


def create_mcp_server() -> MCPServer:
    mcp = MCPServer()
    register_cosmos_tools(mcp)
    history_provider = (
        McpCosmosHistoryProvider(mcp)
        if settings.history_provider == "cosmos"
        else JsonHistoryProvider(settings.local_history_path)
    )
    session_manager = SessionManager(history_provider, summarize_fn=FoundryClient().summarize_history)
    search_client = AzureSearchClient()
    register_history_tools(mcp, session_manager)
    register_retrieval_tools(mcp, search_client)
    return mcp


class FunctionChatOrchestratorService:
    def __init__(self) -> None:
        self._local_mcp = create_mcp_server()
        self._remote_mcp_url = settings.mcp_server_url.strip()
        self._use_remote_mcp = os.getenv("FUNCTION_USE_REMOTE_MCP", "true").strip().lower() in {"1", "true", "yes"}
        self._remote_mcp_client: Client | None = None
        self._remote_mcp_opened = False
        self._remote_mcp_lock = asyncio.Lock()
        if self._use_remote_mcp and self._remote_mcp_url:
            self._remote_mcp_client = Client(self._remote_mcp_url, timeout=30)

        self._project_endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT", os.getenv("PROJECT_ENDPOINT", "")).strip()
        self._credential = DefaultAzureCredential()
        self._project_client = AIProjectClient(endpoint=self._project_endpoint, credential=self._credential)
        self._provider = AzureAIProjectAgentProvider(
            project_endpoint=self._project_endpoint,
            credential=self._credential,
        )
        self._provider_opened = False
        self._rebuilder_task: Any = None
        self._retriever_task: Any = None
        self._response_task: Any = None
        self._init_lock = asyncio.Lock()
        self._is_initialized = False
        self._rebuilder_agent_name = os.getenv("QUERY_REBUILDER_AGENT_NAME", "search-query-rebuilder-agent")
        self._retriever_agent_name = os.getenv("RETRIEVAL_AGENT_NAME", "retrieval-prompt-agent")
        self._response_agent_name = os.getenv("RESPONSE_AGENT_NAME", "response-generator-agent")

    async def _ensure_tasks_initialized(self) -> None:
        if self._is_initialized:
            return
        async with self._init_lock:
            if self._is_initialized:
                return
            await self._provider.__aenter__()
            self._provider_opened = True
            try:
                self._project_client.agents.get(agent_name=self._rebuilder_agent_name)
                self._project_client.agents.get(agent_name=self._retriever_agent_name)
                self._project_client.agents.get(agent_name=self._response_agent_name)
                self._rebuilder_task = await self._provider.get_agent(name=self._rebuilder_agent_name)
                self._retriever_task = await self._provider.get_agent(name=self._retriever_agent_name)
                self._response_task = await self._provider.get_agent(name=self._response_agent_name)
            except Exception as exc:
                raise RuntimeError(
                    "Unable to load Foundry prompt agents. Ensure these names exist: "
                    f"{self._rebuilder_agent_name}, {self._retriever_agent_name}, {self._response_agent_name}."
                ) from exc
            self._is_initialized = True

    async def _ensure_remote_mcp_initialized(self) -> None:
        if self._remote_mcp_client is None or self._remote_mcp_opened:
            return
        async with self._remote_mcp_lock:
            if self._remote_mcp_opened:
                return
            await self._remote_mcp_client.__aenter__()
            self._remote_mcp_opened = True

    async def close(self) -> None:
        if self._provider_opened:
            await self._provider.__aexit__(None, None, None)
            self._provider_opened = False
        if self._remote_mcp_opened and self._remote_mcp_client is not None:
            await self._remote_mcp_client.__aexit__(None, None, None)
            self._remote_mcp_opened = False

    def _parse_mcp_tool_result(self, result: Any, name: str) -> dict[str, Any]:
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        return {"ok": False, "error": f"tool_result_unhandled:{name}"}

    async def _mcp_call(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._remote_mcp_client is not None:
            try:
                await self._ensure_remote_mcp_initialized()
                result = await self._remote_mcp_client.call_tool(name, {"payload": payload}, raise_on_error=False)
                parsed = self._parse_mcp_tool_result(result, name)
                if parsed.get("ok"):
                    return parsed
            except Exception as exc:
                return {"ok": False, "error": f"remote_tool_call_failed:{name}", "details": str(exc)}
        return self._local_mcp.call(name, payload)

    def _extract_text(self, response: Any) -> str:
        value = getattr(response, "value", None)
        if value is not None and hasattr(value, "response"):
            return str(value.response)
        messages = getattr(response, "messages", None)
        if isinstance(messages, list) and messages:
            return str(getattr(messages[0], "text", "") or "")
        if messages is not None:
            return str(getattr(messages, "text", "") or "")
        return str(response)

    def _parse_structured_text(self, text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(stripped)
            except (SyntaxError, ValueError):
                return None

    async def _persist_turn_pair(self, session_id: str, user_query: str, assistant_answer: str) -> None:
        await self._mcp_call("history.append_turn", {"session_id": session_id, "role": "user", "content": user_query})
        await self._mcp_call("history.append_turn", {"session_id": session_id, "role": "assistant", "content": assistant_answer})
        await self._mcp_call("history.get_context", {"session_id": session_id})

    async def _run_prompt_task(self, task: Any, payload: dict[str, Any]) -> str:
        response = await task.run(messages=json.dumps(payload, ensure_ascii=True))
        return self._extract_text(response)

    def _normalize_citations(self, citations: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chunk_citation_by_id = {
            str(chunk.get("chunk_id", "")): chunk.get("citation")
            for chunk in chunks
            if isinstance(chunk, dict) and isinstance(chunk.get("citation"), dict)
        }
        normalized: list[dict[str, Any]] = []
        for item in citations:
            chunk_id = str(item.get("chunk_id") or "").strip()
            if not chunk_id or chunk_id not in chunk_citation_by_id:
                continue
            citation = chunk_citation_by_id[chunk_id]
            source = str(item.get("source") or citation.get("source") or "")
            title = str(item.get("title") or citation.get("title") or "")
            try:
                score = float(citation.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            normalized.append(
                {
                    "chunk_id": chunk_id,
                    "source": source,
                    "title": title,
                    "score": score,
                    "url": source,
                }
            )
        return normalized

    def _elapsed_ms(self, start_ts: float) -> int:
        return int((time.perf_counter() - start_ts) * 1000)

    async def process(self, payload: ChatRequest) -> ChatHttpResponse:
        total_start = time.perf_counter()
        stage_latency_ms: dict[str, int] = {}

        ensure_start = time.perf_counter()
        ensure_result = await self._mcp_call("history.ensure_session", {"session_id": payload.session_id})
        stage_latency_ms["ensure_session"] = self._elapsed_ms(ensure_start)
        if not ensure_result.get("ok"):
            raise RuntimeError("Failed to initialize or load session.")
        session_action = str(ensure_result.get("data", {}).get("action") or "reused")

        init_start = time.perf_counter()
        await self._ensure_tasks_initialized()
        stage_latency_ms["task_initialization"] = self._elapsed_ms(init_start)

        context_start = time.perf_counter()
        ctx_result = await self._mcp_call("history.get_context", {"session_id": payload.session_id})
        stage_latency_ms["load_session_context"] = self._elapsed_ms(context_start)
        ctx = ctx_result.get("data", {}) if isinstance(ctx_result, dict) else {}
        chat_history = [
            {"role": str(turn.get("role") or ""), "content": str(turn.get("content") or "")}
            for turn in ctx.get("recent_window", [])
            if isinstance(turn, dict)
        ]

        trace: dict[str, Any] = {
            "session_loaded": True,
            "query_rebuilt": False,
            "retrieval_attempted": False,
            "chunk_count": 0,
            "response_generated": False,
            "history_persisted": False,
            "mcp_mode": "remote" if self._remote_mcp_client is not None else "local",
        }

        rebuilder_start = time.perf_counter()
        rebuilt_raw = await self._run_prompt_task(
            self._rebuilder_task,
            {
                "summary": str(ctx.get("summary") or ""),
                "chat_history": chat_history,
                "latest_user_query": payload.user_query,
            },
        )
        stage_latency_ms["query_rebuilder_task"] = self._elapsed_ms(rebuilder_start)
        rebuilt_parsed = self._parse_structured_text(rebuilt_raw)
        rebuilt_query = payload.user_query
        if isinstance(rebuilt_parsed, dict):
            rebuilt_query = str(rebuilt_parsed.get("query") or payload.user_query).strip() or payload.user_query
        elif rebuilt_raw.strip() and rebuilt_raw.strip() != "INVALID_INPUT":
            rebuilt_query = rebuilt_raw.strip()
        trace["query_rebuilt"] = rebuilt_query != payload.user_query
        trace["original_query"] = payload.user_query
        trace["rebuilt_query"] = rebuilt_query

        trace["retrieval_attempted"] = True
        retrieval_start = time.perf_counter()
        retrieval_raw = await self._run_prompt_task(
            self._retriever_task,
            {
                "payload": {
                    "query": rebuilt_query,
                    "select": ["id", "source", "filename", "description_data", "metadata"],
                    "filters": None,
                    "top_k": settings.retrieval_top_k,
                }
            },
        )
        stage_latency_ms["retrieval_task"] = self._elapsed_ms(retrieval_start)
        retrieval_parsed = self._parse_structured_text(retrieval_raw)
        retrieval_resp = retrieval_parsed if isinstance(retrieval_parsed, dict) else {}
        if not retrieval_resp.get("ok"):
            answer = "I could not retrieve grounding evidence right now, so I cannot provide a factual answer."
            trace["error_category"] = "retrieval_failure"
            trace["retrieval_error"] = retrieval_raw[:300]
            await self._persist_turn_pair(payload.session_id, payload.user_query, answer)
            trace["history_persisted"] = True
            trace["session_action"] = session_action
            trace["task_order"] = [
                self._rebuilder_agent_name,
                self._retriever_agent_name,
                self._response_agent_name,
            ]
            stage_latency_ms["total"] = self._elapsed_ms(total_start)
            trace["stage_latency_ms"] = stage_latency_ms
            return ChatHttpResponse(
                session_id=payload.session_id,
                answer=answer,
                citations=[],
                grounded=False,
                trace=trace,
                evaluator={"error_category": "retrieval_failure"},
            )

        chunks = retrieval_resp.get("data", {}).get("chunks", []) if isinstance(retrieval_resp, dict) else []
        if not isinstance(chunks, list):
            chunks = []
        trace["chunk_count"] = len(chunks)
        if not chunks:
            answer = "I found no usable retrieval evidence for this query, so I cannot provide a grounded factual answer."
            trace["error_category"] = "retrieval_empty"
            await self._persist_turn_pair(payload.session_id, payload.user_query, answer)
            trace["history_persisted"] = True
            trace["session_action"] = session_action
            trace["task_order"] = [
                self._rebuilder_agent_name,
                self._retriever_agent_name,
                self._response_agent_name,
            ]
            stage_latency_ms["total"] = self._elapsed_ms(total_start)
            trace["stage_latency_ms"] = stage_latency_ms
            return ChatHttpResponse(
                session_id=payload.session_id,
                answer=answer,
                citations=[],
                grounded=False,
                trace=trace,
                evaluator={"error_category": "retrieval_empty"},
            )

        response_start = time.perf_counter()
        response_raw = await self._run_prompt_task(
            self._response_task,
            {
                "chat_history": chat_history,
                "retrieved_chunks": chunks,
                "user_query": payload.user_query,
            },
        )
        stage_latency_ms["response_generator_task"] = self._elapsed_ms(response_start)
        response_parsed = self._parse_structured_text(response_raw)
        response_obj = response_parsed if isinstance(response_parsed, dict) else {}
        answer = str(response_obj.get("answer") or "").strip() or "I could not generate a grounded response from the retrieved context."
        raw_citations = response_obj.get("citations") if isinstance(response_obj.get("citations"), list) else []
        citation_items = [item for item in raw_citations if isinstance(item, dict)]
        citations = self._normalize_citations(citation_items, chunks)
        grounded = bool(citations and answer)

        await self._persist_turn_pair(payload.session_id, payload.user_query, answer)
        trace["response_generated"] = True
        trace["citation_count"] = len(citations)
        trace["history_persisted"] = True
        trace["session_action"] = session_action
        trace["task_order"] = [
            self._rebuilder_agent_name,
            self._retriever_agent_name,
            self._response_agent_name,
        ]
        stage_latency_ms["total"] = self._elapsed_ms(total_start)
        trace["stage_latency_ms"] = stage_latency_ms
        return ChatHttpResponse(
            session_id=payload.session_id,
            answer=answer,
            citations=citations,
            grounded=grounded,
            trace=trace,
            evaluator={},
        )