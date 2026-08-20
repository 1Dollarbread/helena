"""Request/response models for the HELENA server API.

The chat shape deliberately mirrors the OpenAI/Ollama function-calling
convention (messages with roles, `tools` as JSON-schema function specs,
assistant messages carrying `tool_calls`), so any client that already speaks
that dialect — including the terminal harness in this repo — works unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCallFunction(BaseModel):
    name: str
    # Ollama returns already-parsed objects here; OpenAI-style clients send a
    # JSON string. Accept both and let the harness normalize.
    arguments: dict[str, Any] | str = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str | None = None
    type: Literal["function"] = "function"
    function: ToolCallFunction


class Message(BaseModel):
    role: Role
    content: str = ""
    # Bare base64 strings (no data: prefix) — how Ollama vision models take
    # image input. Attached to the message they belong to.
    images: list[str] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    # Present on tool messages so weaker local models can see which tool a
    # result came from even when they ignore tool_call_id linkage.
    name: str | None = None


class ToolSpec(BaseModel):
    type: Literal["function"] = "function"
    function: dict[str, Any]


class GenerationOptions(BaseModel):
    """Per-request sampling/context overrides, passed through to Ollama."""

    num_ctx: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    seed: int | None = None
    num_predict: int | None = None
    repeat_penalty: float | None = None
    stop: list[str] | None = None

    def to_ollama(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    tools: list[ToolSpec] | None = None
    stream: bool = False
    options: GenerationOptions | None = None
    # When set, the exchange is appended to that stored session and the
    # session's prior messages are prepended to `messages`.
    session_id: str | None = None
    # Ollama's structured-output mode: "json" or a JSON schema object.
    format: str | dict[str, Any] | None = None
    keep_alive: str | None = None


class ChatUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    # Nanoseconds from Ollama, surfaced as seconds for readability.
    load_seconds: float | None = None
    prompt_seconds: float | None = None
    eval_seconds: float | None = None
    total_seconds: float | None = None
    tokens_per_second: float | None = None


class ChatResponse(BaseModel):
    model: str
    message: Message
    done_reason: str | None = None
    usage: ChatUsage = Field(default_factory=ChatUsage)
    session_id: str | None = None


class EmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str | None = None


class EmbeddingsResponse(BaseModel):
    model: str
    embeddings: list[list[float]]
    dimensions: int


class VisionRequest(BaseModel):
    """JSON variant of the vision endpoint (base64 images already in hand)."""

    images: list[str]
    prompt: str = "Describe this image in detail."
    model: str | None = None
    system: str | None = None
    options: GenerationOptions | None = None


class ModelInfo(BaseModel):
    name: str
    size: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    family: str | None = None
    modified_at: str | None = None
    # Inferred from the model family/name; Ollama does not always report
    # capabilities directly on /api/tags.
    supports_vision: bool = False
    supports_tools: bool = False


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
    default_model: str
    vision_model: str
    embed_model: str


class PullRequest(BaseModel):
    name: str
    insecure: bool = False


class HealthResponse(BaseModel):
    ok: bool
    version: str
    ollama_reachable: bool
    ollama_host: str
    ollama_version: str | None = None
    model_count: int | None = None
    default_model: str
    detail: str | None = None


class SessionSummary(BaseModel):
    id: str
    title: str | None = None
    model: str | None = None
    message_count: int
    created_at: str
    updated_at: str


class SessionDetail(SessionSummary):
    messages: list[Message]


class CreateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    messages: list[Message] = Field(default_factory=list)


class AppendMessagesRequest(BaseModel):
    messages: list[Message]
