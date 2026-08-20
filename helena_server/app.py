"""FastAPI application: the only thing in this repo that talks to Ollama.

Design notes
------------
* Streaming is exposed as SSE (`text/event-stream`) even though Ollama speaks
  NDJSON, because SSE is what browsers and most HTTP clients handle natively.
  Every SSE payload is a JSON object with a `type` discriminator.
* Tool calling is passed straight through. The server never executes a tool —
  it reports what the model asked for and lets the client decide. That
  separation is what makes the permission system in the harness meaningful.
* Sessions are optional. Pass `session_id` and the server prepends stored
  history and persists the new turn; omit it and the server is stateless.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import Settings, get_settings
from .ollama import OllamaClient, OllamaError, infer_capabilities
from .schemas import (
    AppendMessagesRequest,
    ChatRequest,
    ChatResponse,
    ChatUsage,
    CreateSessionRequest,
    EmbeddingsRequest,
    EmbeddingsResponse,
    HealthResponse,
    Message,
    ModelInfo,
    ModelsResponse,
    PullRequest,
    SessionDetail,
    SessionSummary,
    ToolCall,
    ToolCallFunction,
    VisionRequest,
)
from .store import SessionStore

NS_PER_S = 1_000_000_000


# --- conversions ------------------------------------------------------------


def to_ollama_message(msg: Message) -> dict[str, Any]:
    """Convert an API message into the shape Ollama's /api/chat expects."""
    out: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
    if msg.images:
        out["images"] = msg.images
    if msg.tool_calls:
        calls = []
        for call in msg.tool_calls:
            args = call.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append({"function": {"name": call.function.name, "arguments": args}})
        out["tool_calls"] = calls
    if msg.role == "tool":
        # Ollama keys tool results off `tool_name`; OpenAI-style clients send
        # `name`/`tool_call_id`. Emitting all three keeps every model happy,
        # and the extras are ignored by implementations that don't want them.
        if msg.name:
            out["tool_name"] = msg.name
            out["name"] = msg.name
        if msg.tool_call_id:
            out["tool_call_id"] = msg.tool_call_id
    return out


def from_ollama_message(raw: dict[str, Any]) -> Message:
    tool_calls = None
    if raw.get("tool_calls"):
        tool_calls = []
        for i, call in enumerate(raw["tool_calls"]):
            fn = call.get("function", {})
            tool_calls.append(
                ToolCall(
                    id=call.get("id") or f"call_{i}",
                    function=ToolCallFunction(
                        name=fn.get("name", ""), arguments=fn.get("arguments", {}) or {}
                    ),
                )
            )
    return Message(
        role=raw.get("role", "assistant"),
        content=raw.get("content", "") or "",
        tool_calls=tool_calls,
    )


def usage_from(raw: dict[str, Any]) -> ChatUsage:
    def secs(key: str) -> float | None:
        val = raw.get(key)
        return round(val / NS_PER_S, 3) if isinstance(val, (int, float)) else None

    prompt = raw.get("prompt_eval_count")
    completion = raw.get("eval_count")
    eval_s = secs("eval_duration")
    tps = round(completion / eval_s, 1) if completion and eval_s else None
    return ChatUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=(prompt or 0) + (completion or 0) if (prompt or completion) else None,
        load_seconds=secs("load_duration"),
        prompt_seconds=secs("prompt_eval_duration"),
        eval_seconds=eval_s,
        total_seconds=secs("total_duration"),
        tokens_per_second=tps,
    )


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# --- app --------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    app.state.settings = settings
    app.state.ollama = OllamaClient(settings.ollama_host, timeout=settings.request_timeout)
    app.state.store = SessionStore(settings.resolved_db_path)
    app.state.semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))
    try:
        yield
    finally:
        await app.state.ollama.aclose()
        app.state.store.close()


app = FastAPI(
    title="H.E.L.E.N.A model server",
    description="A local-first FastAPI front end for Ollama: chat, tool calling, vision, embeddings, and persisted sessions.",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(OllamaError)
async def _ollama_error_handler(_: Request, exc: OllamaError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


def settings_dep() -> Settings:
    return get_settings()


async def require_auth(request: Request) -> None:
    """Bearer-token gate. A blank token (the default) disables auth entirely."""
    token = get_settings().api_token
    if not token:
        return
    header = request.headers.get("authorization", "")
    provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if provided != token:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")


def client(request: Request) -> OllamaClient:
    return request.app.state.ollama


def store(request: Request) -> SessionStore:
    return request.app.state.store


# --- health & models --------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health(request: Request, settings: Settings = Depends(settings_dep)) -> HealthResponse:
    oc: OllamaClient = client(request)
    version = await oc.version()
    if version is None:
        return HealthResponse(
            ok=False,
            version=__version__,
            ollama_reachable=False,
            ollama_host=settings.ollama_host,
            default_model=settings.default_model,
            detail=(
                f"Ollama is not answering at {settings.ollama_host}. "
                "Install it from https://ollama.com and leave `ollama serve` running."
            ),
        )
    try:
        models = await oc.list_models()
        count = len(models)
    except OllamaError:
        count = None
    return HealthResponse(
        ok=True,
        version=__version__,
        ollama_reachable=True,
        ollama_host=settings.ollama_host,
        ollama_version=version,
        model_count=count,
        default_model=settings.default_model,
    )


@app.get("/v1/models", response_model=ModelsResponse, dependencies=[Depends(require_auth)])
async def list_models(request: Request, settings: Settings = Depends(settings_dep)) -> ModelsResponse:
    raw = await client(request).list_models()
    models = []
    for m in raw:
        details = m.get("details") or {}
        name = m.get("name") or m.get("model") or "?"
        families = details.get("families") or ([details["family"]] if details.get("family") else [])
        vision, tools = infer_capabilities(name, families)
        models.append(
            ModelInfo(
                name=name,
                size=m.get("size"),
                parameter_size=details.get("parameter_size"),
                quantization=details.get("quantization_level"),
                family=details.get("family"),
                modified_at=m.get("modified_at"),
                supports_vision=vision,
                supports_tools=tools,
            )
        )
    models.sort(key=lambda m: m.name)
    return ModelsResponse(
        models=models,
        default_model=settings.default_model,
        vision_model=settings.vision_model,
        embed_model=settings.embed_model,
    )


@app.get("/v1/models/{name:path}/info", dependencies=[Depends(require_auth)])
async def model_info(name: str, request: Request) -> dict[str, Any]:
    data = await client(request).show(name)
    # `show` includes the full modelfile and template, which is a lot of noise
    # for a client that only wants to know what it's working with.
    return {
        "name": name,
        "details": data.get("details"),
        "capabilities": data.get("capabilities"),
        "parameters": data.get("parameters"),
        "context_length": (data.get("model_info") or {}).get("general.context_length"),
    }


@app.post("/v1/models/pull", dependencies=[Depends(require_auth)])
async def pull_model(req: PullRequest, request: Request) -> StreamingResponse:
    oc: OllamaClient = client(request)

    async def gen() -> AsyncIterator[str]:
        try:
            async for chunk in oc.pull(req.name, req.insecure):
                total, completed = chunk.get("total"), chunk.get("completed")
                percent = round(completed / total * 100, 1) if total and completed else None
                yield sse({
                    "type": "progress",
                    "status": chunk.get("status", ""),
                    "total": total,
                    "completed": completed,
                    "percent": percent,
                })
            yield sse({"type": "done", "name": req.name})
        except OllamaError as exc:
            yield sse({"type": "error", "error": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.delete("/v1/models/{name:path}", dependencies=[Depends(require_auth)])
async def delete_model(name: str, request: Request) -> dict[str, Any]:
    await client(request).delete_model(name)
    return {"deleted": name}


# --- chat -------------------------------------------------------------------


async def _resolve_history(
    req: ChatRequest, st: SessionStore
) -> tuple[list[dict[str, Any]], str | None]:
    """Prepend stored session history, if a session was named."""
    outgoing = [to_ollama_message(m) for m in req.messages]
    if not req.session_id:
        return outgoing, None
    try:
        prior = await st.messages(req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No session {req.session_id!r}.")
    return prior + outgoing, req.session_id


@app.post("/v1/chat", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def chat(
    req: ChatRequest, request: Request, settings: Settings = Depends(settings_dep)
):
    """One completion. Set `stream: true` for an SSE stream instead of JSON."""
    if req.stream:
        return await chat_stream(req, request, settings)

    oc: OllamaClient = client(request)
    st: SessionStore = store(request)
    model = req.model or settings.default_model
    messages, session_id = await _resolve_history(req, st)

    options = (req.options.to_ollama() if req.options else {})
    options.setdefault("num_ctx", settings.num_ctx)
    options.setdefault("temperature", settings.temperature)
    tools = [t.model_dump() for t in req.tools] if req.tools else None

    async with request.app.state.semaphore:
        raw = await oc.chat(
            model=model, messages=messages, tools=tools, options=options,
            fmt=req.format, keep_alive=req.keep_alive,
        )

    message = from_ollama_message(raw.get("message") or {})
    if session_id:
        await st.append(session_id, [to_ollama_message(m) for m in req.messages])
        await st.append(session_id, [to_ollama_message(message)])

    return ChatResponse(
        model=raw.get("model", model),
        message=message,
        done_reason=raw.get("done_reason"),
        usage=usage_from(raw),
        session_id=session_id,
    )


@app.post("/v1/chat/stream", dependencies=[Depends(require_auth)])
async def chat_stream(
    req: ChatRequest, request: Request, settings: Settings = Depends(settings_dep)
) -> StreamingResponse:
    """SSE stream of a completion.

    Event types: `token` (a text delta), `tool_calls` (the model wants to act —
    always the last thing before `done` for that turn), `done` (with usage),
    and `error`.
    """
    oc: OllamaClient = client(request)
    st: SessionStore = store(request)
    model = req.model or settings.default_model
    messages, session_id = await _resolve_history(req, st)

    options = (req.options.to_ollama() if req.options else {})
    options.setdefault("num_ctx", settings.num_ctx)
    options.setdefault("temperature", settings.temperature)
    tools = [t.model_dump() for t in req.tools] if req.tools else None
    incoming = [to_ollama_message(m) for m in req.messages]

    async def gen() -> AsyncIterator[str]:
        started = time.monotonic()
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        try:
            async with request.app.state.semaphore:
                async for chunk in oc.chat_stream(
                    model=model, messages=messages, tools=tools, options=options,
                    fmt=req.format, keep_alive=req.keep_alive,
                ):
                    msg = chunk.get("message") or {}
                    piece = msg.get("content")
                    if piece:
                        text_parts.append(piece)
                        yield sse({"type": "token", "text": piece})
                    # Ollama emits tool calls as whole objects (not deltas),
                    # usually in the last chunk before done.
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        final = chunk
        except OllamaError as exc:
            yield sse({"type": "error", "error": str(exc)})
            return
        except asyncio.CancelledError:
            # Client hung up mid-stream (Ctrl-C in the harness). Nothing to
            # report back over a dead socket; just stop cleanly.
            raise

        message = from_ollama_message(
            {"role": "assistant", "content": "".join(text_parts), "tool_calls": tool_calls}
        )
        if tool_calls:
            yield sse({
                "type": "tool_calls",
                "tool_calls": [tc.model_dump() for tc in (message.tool_calls or [])],
            })

        if session_id:
            await st.append(session_id, incoming)
            await st.append(session_id, [to_ollama_message(message)])

        usage = usage_from(final)
        if usage.total_seconds is None:
            usage.total_seconds = round(time.monotonic() - started, 3)
        yield sse({
            "type": "done",
            "model": final.get("model", model),
            "done_reason": final.get("done_reason"),
            "content": message.content,
            "usage": usage.model_dump(),
            "session_id": session_id,
        })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- vision -----------------------------------------------------------------


@app.post("/v1/vision", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def vision(
    req: VisionRequest, request: Request, settings: Settings = Depends(settings_dep)
) -> ChatResponse:
    """Ask a multimodal model about one or more base64-encoded images."""
    oc: OllamaClient = client(request)
    model = req.model or settings.vision_model
    messages: list[dict[str, Any]] = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    messages.append({"role": "user", "content": req.prompt, "images": req.images})

    options = (req.options.to_ollama() if req.options else {})
    options.setdefault("num_ctx", settings.num_ctx)

    async with request.app.state.semaphore:
        raw = await oc.chat(model=model, messages=messages, options=options)
    return ChatResponse(
        model=raw.get("model", model),
        message=from_ollama_message(raw.get("message") or {}),
        done_reason=raw.get("done_reason"),
        usage=usage_from(raw),
    )


@app.post("/v1/vision/upload", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def vision_upload(
    request: Request,
    file: UploadFile = File(..., description="An image file (png, jpg, gif, webp, bmp)."),
    prompt: str = Form("Describe this image in detail."),
    model: str | None = Form(None),
    settings: Settings = Depends(settings_dep),
) -> ChatResponse:
    """Multipart image upload, for clients that would rather not base64 by hand."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image is larger than 20 MB.")
    encoded = base64.b64encode(data).decode("ascii")
    return await vision(
        VisionRequest(images=[encoded], prompt=prompt, model=model), request, settings
    )


# --- embeddings -------------------------------------------------------------


@app.post("/v1/embeddings", response_model=EmbeddingsResponse, dependencies=[Depends(require_auth)])
async def embeddings(
    req: EmbeddingsRequest, request: Request, settings: Settings = Depends(settings_dep)
) -> EmbeddingsResponse:
    model = req.model or settings.embed_model
    inputs = [req.input] if isinstance(req.input, str) else list(req.input)
    if not inputs:
        raise HTTPException(status_code=400, detail="`input` must not be empty.")
    async with request.app.state.semaphore:
        vectors = await client(request).embed(model=model, inputs=inputs)
    return EmbeddingsResponse(
        model=model, embeddings=vectors, dimensions=len(vectors[0]) if vectors else 0
    )


# --- sessions ---------------------------------------------------------------


@app.post("/v1/sessions", response_model=SessionSummary, dependencies=[Depends(require_auth)])
async def create_session(req: CreateSessionRequest, request: Request) -> SessionSummary:
    st: SessionStore = store(request)
    sid = await st.create(
        title=req.title, model=req.model,
        messages=[to_ollama_message(m) for m in req.messages],
    )
    detail = await st.get(sid)
    detail.pop("messages", None)
    return SessionSummary(**detail)


@app.get("/v1/sessions", response_model=list[SessionSummary], dependencies=[Depends(require_auth)])
async def list_sessions(request: Request, limit: int = 50) -> list[SessionSummary]:
    return [SessionSummary(**row) for row in await store(request).list(limit)]


@app.get("/v1/sessions/{session_id}", response_model=SessionDetail, dependencies=[Depends(require_auth)])
async def get_session(session_id: str, request: Request) -> SessionDetail:
    try:
        detail = await store(request).get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}.")
    return SessionDetail(**detail)


@app.post("/v1/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
async def append_messages(
    session_id: str, req: AppendMessagesRequest, request: Request
) -> dict[str, Any]:
    try:
        added = await store(request).append(
            session_id, [to_ollama_message(m) for m in req.messages]
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}.")
    return {"session_id": session_id, "added": added}


@app.patch("/v1/sessions/{session_id}", response_model=SessionSummary, dependencies=[Depends(require_auth)])
async def rename_session(session_id: str, title: str, request: Request) -> SessionSummary:
    st: SessionStore = store(request)
    try:
        await st.set_title(session_id, title)
        detail = await st.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}.")
    detail.pop("messages", None)
    return SessionSummary(**detail)


@app.delete("/v1/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    try:
        await store(request).delete(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}.")
    return {"deleted": session_id}
