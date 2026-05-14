"""
LLM Wrapper - Proxies requests to your local LLM while hiding the model name.
Users connect to port 8001; requests are forwarded to the real LLM on port 8000.
"""

import asyncio
import json
import os
import re
import threading
import time
import uuid
import logging
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

# ============== Configuration ==============
# Upstream LLM (your actual model)
INTERNAL_URL = os.environ.get("INTERNAL_LLM_URL", "http://192.168.30.239:7000/v1/chat/completions")
INTERNAL_MODEL = os.environ.get("INTERNAL_MODEL", "google/gemma-3-4b-it")

# Public-facing identity (what users see)
PUBLIC_MODEL_NAME = os.environ.get("PUBLIC_MODEL_NAME", "surya-01")

# Optional system guard - set to None or "" to disable (preserves max accuracy)
# When set, prepends this to prevent model from revealing its identity.
# BACKEND_IDENTITY: when probed about model/identity, present as this (e.g. Mistral 32B)
BACKEND_IDENTITY = os.environ.get("BACKEND_IDENTITY", "DONT KNOW")
SYSTEM_GUARD_CONTENT = os.environ.get(
    "SYSTEM_GUARD",
    f"You are a helpful assistant. Your name is {PUBLIC_MODEL_NAME}. "
    f"If someone asks about your architecture, training, or creator, respond that you are {BACKEND_IDENTITY} and cannot share further details. "
    f"For all other requests, respond normally and helpfully."
)
# Gemma does not support role "system" — inject as a user/assistant turn instead
SYSTEM_GUARD = [
    {"role": "user",      "content": SYSTEM_GUARD_CONTENT},
    {"role": "assistant", "content": "Understood. I will follow these instructions."},
] if SYSTEM_GUARD_CONTENT else []

# Patterns that might leak the real model name (case-insensitive)
# Redact ALL GPT-related and OpenAI references - user sees only BACKEND_IDENTITY (e.g. Mistral 32B)
# Use [-‑] to match both regular hyphen and Unicode en-dash
SENSITIVE_PATTERNS = [
    r"gpt[-‑]?oss",
    r"gpt_oss",
    r"gptoss",
    r"openai/gpt[-‑]?oss[-‑]?\d*",
    r"\bGPT[-‑]?\d*\.?\d*[-‑]?\w*",   # GPT-4, GPT-3.5, GPT-3, GPT-2, GPT-3.5-turbo
    r"\bGPT\s+family\b",
    r"\bGPT\s+models\b",
    r"\bGPT\s+tokenizer\b",
    r"\bGPT\b",                         # standalone GPT
    r"\bOpenAI\b",
    r"OpenAI's",
]
SENSITIVE_RE = re.compile("|".join(SENSITIVE_PATTERNS), re.IGNORECASE)

# Probe patterns - if user asks these, return canned response
PROBE_PHRASES = [
    "what model are you",
    "what's your model",
    "which model",
    "your model name",
    "model name",
    "identify yourself",
    "who are you",
    "what are you",
    "system prompt",
    "internal model",
]

# ============== Logger ==============
_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_FILE_DEFAULT = os.path.join(_HERE, "logs", "requests.log")
LOG_FILE  = os.environ.get("LOG_FILE", _LOG_FILE_DEFAULT)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = dict(record.__dict__.get("payload") or {})
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        return json.dumps(payload, ensure_ascii=False, default=str)


def _make_rotating_handler(path: str, level: str) -> TimedRotatingFileHandler:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = TimedRotatingFileHandler(path, when="d", backupCount=30, utc=True)
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(_JsonFormatter())
    fh.setLevel(level)
    return fh


def _build_loggers():
    fmt = _JsonFormatter()

    # calls logger → requests.log (one line per completed call)
    calls_lg = logging.getLogger("wrapper.calls")
    calls_lg.setLevel(LOG_LEVEL)
    calls_lg.propagate = False
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    calls_lg.addHandler(sh)
    if LOG_FILE:
        calls_lg.addHandler(_make_rotating_handler(LOG_FILE, LOG_LEVEL))

    # errors logger → errors.log (only failures and probe attempts)
    err_file = LOG_FILE.replace("requests.log", "errors.log") if LOG_FILE else None
    err_lg = logging.getLogger("wrapper.errors")
    err_lg.setLevel("WARNING")
    err_lg.propagate = False
    if err_file:
        err_lg.addHandler(_make_rotating_handler(err_file, "WARNING"))

    return calls_lg, err_lg


logger, error_logger = _build_loggers()
logger.info("", extra={"payload": {"event": "startup", "internal_url": INTERNAL_URL, "model": INTERNAL_MODEL}})

# ============== Call Stats ==============
_stats_lock = threading.Lock()
_daily_counts: Dict[str, int] = {}


def _current_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _record_call():
    day = _current_day()
    with _stats_lock:
        _daily_counts[day] = _daily_counts.get(day, 0) + 1


def _log_call(req_id: str, client_ip: str, stream: bool, status: int,
              elapsed_ms: int, user_messages: List[Dict],
              output: str, usage: dict = None):
    entry = {
        "req_id": req_id,
        "client_ip": client_ip,
        "stream": stream,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "input": user_messages,
        "output": output,
    }
    if usage:
        entry["tokens"] = usage
    logger.info("", extra={"payload": entry})


def _log_error(payload: dict):
    error_logger.warning("", extra={"payload": payload})


app = FastAPI(title="Chat API", version="1.0", docs_url="/docs", redoc_url="/redoc")
_sem = asyncio.Semaphore(4)

@app.middleware("http")
async def hide_client_identity(request, call_next):
    """Strip OpenAI-related headers so clients cannot detect OpenAI compatibility."""
    response = await call_next(request)
    for k in list(response.headers.keys()):
        if "openai" in k.lower():
            response.headers.pop(k, None)
    return response


def is_probe_attempt(messages: List[Dict[str, Any]]) -> bool:
    """Detect if the user is trying to extract the model name or system prompt."""
    text = " ".join(
        str(m.get("content", ""))
        for m in messages
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    ).lower()
    return any(phrase in text for phrase in PROBE_PHRASES)


def sanitize_output(content: str) -> str:
    """Redact any leaked model identifiers from the response."""
    if not content or not isinstance(content, str):
        return content
    return SENSITIVE_RE.sub("[redacted]", content)


def convert_system_messages(messages: List[Dict]) -> List[Dict]:
    """
    Gemma does not support role='system'. Convert any system messages to a
    user/assistant turn pair so the chat template doesn't throw an error.
    """
    converted = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            converted.append({"role": "user",      "content": msg.get("content", "")})
            converted.append({"role": "assistant", "content": "Understood."})
        else:
            converted.append(msg)
    return converted


def build_upstream_payload(body: dict, messages: List[Dict]) -> dict:
    """Build the payload to send to the upstream LLM. Preserves all params for accuracy."""
    payload = {
        "model": INTERNAL_MODEL,
        "messages": convert_system_messages(messages),
        "stream": body.get("stream", False),
    }
    # Pass through common params - no accuracy loss
    for key in ("temperature", "max_tokens", "top_p", "top_k", "frequency_penalty", "presence_penalty", "stop", "n"):
        if key in body and body[key] is not None:
            payload[key] = body[key]
    # Temperature bounds
    if "temperature" in payload:
        try:
            t = float(payload["temperature"])
            payload["temperature"] = max(0.0, min(2.0, t))
        except (TypeError, ValueError):
            payload["temperature"] = 0.7
    return payload


def sanitize_response(data: dict) -> dict:
    """Replace model name and filter response content."""
    data["model"] = PUBLIC_MODEL_NAME
    for k in ["prompt_logprobs", "prompt_token_ids", "kv_transfer_params", "system_fingerprint", "service_tier"]:
        data.pop(k, None)
    data.pop("usage", None)

    if "choices" in data and isinstance(data["choices"], list):
        sanitized = []
        for choice in data["choices"]:
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") or choice.get("delta") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if content is None and isinstance(msg, dict):
                content = msg.get("reasoning_content") or msg.get("reasoning")
            if content is None and isinstance(choice.get("text"), str):
                content = choice.get("text")
            if isinstance(content, str):
                content = sanitize_output(content)
            else:
                content = ""
            role = msg.get("role", "assistant") if isinstance(msg, dict) else "assistant"
            safe_msg = {"role": role, "content": content}
            sanitized.append({"index": choice.get("index"), "message": safe_msg})
        data["choices"] = sanitized
    return data


@app.post("/v1/chat/completions")
async def secure_chat(request: Request):
    req_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    client_ip = request.client.host if request.client else None
    _record_call()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raw = (await request.body()).decode(errors="replace")
        if not raw.strip():
            raise HTTPException(status_code=400, detail="Empty request body")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_messages: List[Dict[str, Any]] = body.get("messages") or []
    if not isinstance(user_messages, list) or len(user_messages) == 0:
        raise HTTPException(status_code=400, detail="'messages' must be a non-empty list")

    # Block probing for model name
    if is_probe_attempt(user_messages):
        canned = "I'm a helpful AI assistant. I don't share internal implementation details."
        elapsed_probe = round((time.monotonic() - t0) * 1000)
        _log_error({"event": "probe_blocked", "req_id": req_id, "client_ip": client_ip,
                    "elapsed_ms": elapsed_probe, "query": user_messages[-1].get("content", "")[:200]})
        _log_call(req_id, client_ip, False, 200, elapsed_probe, user_messages, canned)
        return {
            "model": PUBLIC_MODEL_NAME,
            "choices": [{"message": {"role": "assistant", "content": canned}}]
        }

    messages = SYSTEM_GUARD + user_messages
    payload = build_upstream_payload(body, messages)

    # Streaming
    if payload.get("stream"):
        async def stream_filter():
            async with _sem:
                full_content: List[str] = []
                upstream_status = None
                try:
                  async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream("POST", INTERNAL_URL, json=payload) as upstream:
                        upstream_status = upstream.status_code
                        if upstream.status_code != 200:
                            err_body = sanitize_output((await upstream.aread()).decode(errors="replace"))
                            _log_error({"event": "upstream_error", "req_id": req_id,
                                        "status": upstream.status_code, "detail": err_body,
                                        "elapsed_ms": round((time.monotonic() - t0) * 1000)})
                            yield (b"data: " + json.dumps({"error": {"message": err_body, "code": str(upstream.status_code)}}).encode() + b"\n\n")
                            return
                        buffer = b""
                        async for chunk in upstream.aiter_bytes():
                            buffer += chunk
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                if line.strip().startswith(b"data: "):
                                    try:
                                        data = json.loads(line[6:].decode())
                                        if data.get("choices"):
                                            for c in data["choices"]:
                                                if isinstance(c.get("delta"), dict) and "content" in c["delta"]:
                                                    c["delta"]["content"] = sanitize_output(c["delta"]["content"] or "")
                                                    full_content.append(c["delta"]["content"])
                                        if "model" in data:
                                            data["model"] = PUBLIC_MODEL_NAME
                                        yield (b"data: " + json.dumps(data).encode() + b"\n\n")
                                    except json.JSONDecodeError:
                                        yield line + b"\n"
                                else:
                                    yield line + b"\n" if not line.endswith(b"\n") else line
                        if buffer:
                            yield buffer
                  _log_call(req_id, client_ip, True, upstream_status or 0,
                            round((time.monotonic() - t0) * 1000),
                            user_messages, "".join(full_content))
                except httpx.ConnectError:
                    elapsed = round((time.monotonic() - t0) * 1000)
                    _log_error({"event": "backend_unreachable", "req_id": req_id,
                                "url": INTERNAL_URL, "elapsed_ms": elapsed})
                    yield (b"data: " + json.dumps({"error": {"message": "LLM backend unreachable", "code": "503"}}).encode() + b"\n\n")

        return StreamingResponse(
            stream_filter(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    # Non-streaming
    try:
        async with _sem:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(INTERNAL_URL, json=payload)
    except httpx.ConnectError:
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        _log_error({"event": "backend_unreachable", "req_id": req_id,
                    "url": INTERNAL_URL, "elapsed_ms": elapsed_ms})
        raise HTTPException(status_code=503, detail="LLM backend unreachable")

    elapsed_ms = round((time.monotonic() - t0) * 1000)

    if response.status_code != 200:
        detail = sanitize_output(response.text)
        _log_error({"event": "upstream_error", "req_id": req_id,
                    "status": response.status_code, "detail": detail, "elapsed_ms": elapsed_ms})
        raise HTTPException(status_code=response.status_code, detail=detail)

    data = response.json()
    usage = data.get("usage")
    output_text = " ".join(
        (c.get("message") or {}).get("content") or ""
        for c in data.get("choices", [])
        if isinstance(c, dict)
    )
    _log_call(req_id, client_ip, False, response.status_code, elapsed_ms,
              user_messages, output_text, usage)
    return sanitize_response(data)


@app.get("/v1/models")
async def list_models():
    """Return a single model so clients don't see the real model name."""
    return {
        "object": "list",
        "data": [
            {
                "id": PUBLIC_MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "custom"
            }
        ]
    }


@app.get("/stats")
async def stats():
    with _stats_lock:
        counts = dict(_daily_counts)
    today = _current_day()
    return {
        "today": today,
        "calls_today": counts.get(today, 0),
        "total_calls": sum(counts.values()),
        "daily_breakdown": counts,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WRAPPER_PORT", "9001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
