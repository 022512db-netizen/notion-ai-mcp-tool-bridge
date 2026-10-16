import time
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

# ================================
# 请求相关 Schema (Chat Completion)
# ================================

class ChatMessage(BaseModel):
    """单条对话消息"""
    role: Literal["user", "assistant", "system"]
    content: str
    thinking: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_role(cls, data):
        # 兼容 tool / developer / function 角色与 assistant.tool_calls：
        # 桥接只认 user/assistant/system，其余折算成文本，避免 422。
        if not isinstance(data, dict):
            return data
        data = dict(data)
        role = data.get("role")
        if role in ("tool", "function"):
            body = data.get("content")
            if isinstance(body, list):
                body = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in body)
            data["role"] = "user"
            data["content"] = "[tool result] " + (body or "")
        elif role == "developer":
            data["role"] = "system"
        elif role == "assistant" and data.get("tool_calls"):
            calls = []
            for c in data["tool_calls"]:
                fn = (c or {}).get("function") or {}
                calls.append(f"[tool call] {fn.get('name', '')}({fn.get('arguments', '')})")
            body = data.get("content")
            if isinstance(body, list):
                body = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in body)
            data["content"] = ((body or "") + "\n" + "\n".join(calls)).strip()
        return data

    @field_validator("content", mode="before")
    @classmethod
    def _coerce_content_parts(cls, v):
        # OpenAI-style content parts (отправляют omp и др. клиенты) → plain text.
        # Не-текстовые части (image_url и т.п.) мост не поддерживает и молча отбрасывает.
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            texts = []
            for part in v:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text") or "")
                elif isinstance(part, str):
                    texts.append(part)
            return "".join(texts)
        return str(v)

class ChatCompletionRequest(BaseModel):
    """
    OpenAI-Compatible 发起完成请求的 Payload。
    保留 `conversation_id` 作为特定的扩展字段，若缺失则视为独立请求。
    """
    model: str = Field(default="claude-opus4.6", description="Requested model.")
    messages: List[ChatMessage]
    stream: bool = Field(default=False, description="Whether to stream the response as SSE.")
    temperature: Optional[float] = Field(default=None, description="Sampling temperature.")
    conversation_id: Optional[str] = Field(default=None, description="Extension for stateful conversation tracking.")
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Any = "auto"


# ================================
# 非流式返回 Schema
# ================================

class ChatMessageResponseChoice(BaseModel):
    """非流式响应的选项"""
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    """
    OpenAI-Compatible 完整返回 Payload。
    """
    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatMessageResponseChoice]
    usage: Dict[str, int] = Field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    # Standard 模式扩展字段
    search_metadata: Optional[Dict[str, Any]] = Field(default=None)

# ================================
# 流式返回 Schema (供内部组织)
# ================================

class ChatCompletionChunkDelta(BaseModel):
    """SSE Delta Block"""
    content: Optional[str] = None
    role: Optional[str] = None

class ChatCompletionChunkChoice(BaseModel):
    """SSE Choice Block"""
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None

class ChatCompletionChunk(BaseModel):
    """
    OpenAI-Compatible 流式 Chunk
    """
    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChunkChoice]
