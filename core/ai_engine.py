"""Unified async streaming client for cloud and local LLM providers.

Provider coverage (single httpx transport, no SDKs):
  * OpenAI-compatible  ``/chat/completions`` SSE (real OpenAI, vLLM, LocalAI)
  * Google Gemini       ``:streamGenerateContent`` SSE
  * Ollama              ``/api/chat`` NDJSON stream

Every provider runs in an ``asyncio`` event loop hosted on a dedicated worker
thread; chunks are forwarded to the GUI as thread-safe Qt signals so the main
event loop never blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from threading import Event
from typing import Awaitable, Callable, Iterable

import httpx
from PySide6.QtCore import QObject, QThread, Signal

from config import ConfigManager

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=8.0, read=90.0, write=30.0, pool=8.0)

# Callbacks used by the transport functions.
ChunkCallback = Callable[[str], None]
ProviderCallback = Callable[[str], None]


class ProviderError(Exception):
    """Base error for provider request failures."""

    def __init__(self, provider: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class ProviderOfflineError(ProviderError):
    """The provider could not be reached (connection refused/timeout/DNS)."""


class NoProviderAvailableError(ProviderError):
    """Every candidate in the fallback chain was offline or unconfigured."""


# ---------------------------------------------------------------------------
# Prompt preparation
# ---------------------------------------------------------------------------

def prepare_prompt(config: ConfigManager, text: str) -> tuple[str, str]:
    """Resolve quick-action system prompts; returns ``(system_prompt, query)``.

    A leading ``/word`` that matches a configured quick action is removed from
    the query and its template becomes the system prompt.
    """
    query = (text or "").strip()
    if not query:
        return str(config.get("system_prompt", "")), ""
    tokens = query.split(None, 1)
    first = tokens[0].lower() if tokens else ""
    actions = config.get("quick_actions", {})
    if first in actions:
        template = str(actions[first])
        rest = tokens[1] if len(tokens) > 1 else ""
        return (template, rest.strip()) if template else (str(template), rest.strip())
    return str(config.get("system_prompt", "")), query


def provider_chain(config: ConfigManager) -> list[str]:
    """Ordered providers to try: active first, then the configured fallbacks."""
    active = str(config.get("provider", "openai"))
    chain: list[str] = []
    for name in (active, *config.get("fallback_order", ["ollama", "openai", "gemini"])):
        if name not in chain:
            chain.append(name)
    return chain


# ---------------------------------------------------------------------------
# Transport implementations
# ---------------------------------------------------------------------------

def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)


def _openai_url(config: ConfigManager, provider: str) -> str:
    pc = config.provider_config(provider)
    base = (pc.get("base_url") or "").rstrip("/")
    return f"{base}/chat/completions"


def _openai_body(config: ConfigManager, provider: str, model: str, messages: list[dict]) -> dict:
    return {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": float(config.get("temperature", 0.7)),
        "max_tokens": int(config.get("max_tokens", 2048)),
    }


async def stream_openai(
    config: ConfigManager,
    provider: str,
    messages: list[dict],
    on_chunk: ChunkCallback,
    *,
    client: httpx.AsyncClient,
    model: str,
) -> str:
    """OpenAI-compatible SSE streaming."""

    key = config.api_key(provider)
    if provider not in ("custom", "ollama") and not key:
        raise ProviderError(provider, "No API key configured for this provider")

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    url = _openai_url(config, provider)
    payload = _openai_body(config, provider, model, messages)
    buffer: list[str] = []
    try:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise ProviderError(
                    provider,
                    f"API error {response.status_code}: {text.decode(errors='replace')[:200]}",
                    status_code=response.status_code,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        buffer.append(delta)
                        on_chunk(delta)
    except httpx.HTTPError as exc:
        raise ProviderOfflineError(provider, f"Connection failed: {exc}") from exc
    return "".join(buffer)


async def stream_gemini(
    config: ConfigManager,
    provider: str,
    messages: list[dict],
    on_chunk: ChunkCallback,
    *,
    client: httpx.AsyncClient,
    model: str,
) -> str:
    """Google Gemini :streamGenerateContent SSE."""

    key = config.api_key(provider)
    if not key:
        raise ProviderError(provider, "No API key configured for Gemini")

    base = config.provider_config(provider).get("base_url") or ""
    base = base.rstrip("/") or "https://generativelanguage.googleapis.com"
    url = f"{base}/v1beta/models/{model}:streamGenerateContent?alt=sse"

    system = ""
    contents: list[dict] = []
    for msg in messages:
        if msg["role"] == "system":
            system += (system and "\n") + msg["content"]
        else:
            gemini_role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})

    body: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": float(config.get("temperature", 0.7)),
            "maxOutputTokens": int(config.get("max_tokens", 2048)),
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    buffer: list[str] = []
    try:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise ProviderError(
                    provider,
                    f"Gemini error {response.status_code}: {text.decode(errors='replace')[:200]}",
                    status_code=response.status_code,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                candidates = obj.get("candidates") or []
                if not candidates:
                    continue
                parts = (candidates[0].get("content") or {}).get("parts") or []
                for part in parts:
                    if part.get("text"):
                        buffer.append(part["text"])
                        on_chunk(part["text"])
    except httpx.HTTPError as exc:
        raise ProviderOfflineError(provider, f"Connection failed: {exc}") from exc
    return "".join(buffer)


async def stream_ollama(
    config: ConfigManager,
    provider: str,
    messages: list[dict],
    on_chunk: ChunkCallback,
    *,
    client: httpx.AsyncClient,
    model: str,
) -> str:
    """Ollama native ``/api/chat`` NDJSON streaming."""

    base = (config.provider_config(provider).get("base_url") or "").rstrip("/")
    url = f"{base}/api/chat"
    body = {
        "model": model,
        "messages": [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] != "system"
        ],
        "stream": True,
    }
    buffer: list[str] = []
    try:
        async with client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise ProviderError(
                    provider,
                    f"Ollama error {response.status_code}: {text.decode(errors='replace')[:200]}",
                    status_code=response.status_code,
                )
            async for line in response.aiter_lines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                message = (obj.get("message") or {}).get("content")
                if message:
                    buffer.append(message)
                    on_chunk(message)
                if obj.get("done"):
                    break
    except httpx.HTTPError as exc:
        raise ProviderOfflineError(provider, f"Connection failed: {exc}") from exc
    return "".join(buffer)


async def run_request(
    config: ConfigManager,
    chain: Iterable[str],
    messages: list[dict],
    on_chunk: ChunkCallback,
    on_provider: ProviderCallback | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Stream from the first reachable provider in `chain`.

    Falls back to the next provider only on ``ProviderOfflineError`` (e.g. a
    local model server that is down); authentication / validation failures
    surface immediately.
    """
    own_client = client is None
    async_client = client or _build_client()
    last_exc: ProviderError | None = None
    try:
        for name in chain:
            pc = config.provider_config(name)
            model = pc.get("model") or config.get("model") or ""
            if not model:
                continue
            try:
                if on_provider:
                    on_provider(name)
                if name == "gemini":
                    full = await stream_gemini(config, name, messages, on_chunk,
                                               client=async_client, model=model)
                elif name == "ollama":
                    full = await stream_ollama(config, name, messages, on_chunk,
                                               client=async_client, model=model)
                else:
                    full = await stream_openai(config, name, messages, on_chunk,
                                               client=async_client, model=model)
                return full, name
            except ProviderOfflineError as exc:
                log.warning("provider %r offline: %s", name, exc)
                last_exc = exc
            except ProviderError:
                raise
    finally:
        if own_client:
            await async_client.aclose()

    if last_exc is None:
        last_exc = NoProviderAvailableError(
            "chain", "No provider in the chain had a usable model configured"
        )
    raise NoProviderAvailableError(
        ",".join(chain),
        f"All providers are offline or unreachable: {last_exc}",
        status_code=getattr(last_exc, "status_code", None),
    ) from last_exc


# ---------------------------------------------------------------------------
# Qt worker thread
# ---------------------------------------------------------------------------

_STOP = object()


class EngineWorker(QThread):
    """Runs an asyncio event loop and drains a FIFO of requests."""

    started = Signal(str)
    chunk = Signal(str)
    done = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, config: ConfigManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._requests: list = []
        self._queue_guard = Event()
        self._stop_flag = False
        self._loop = None
        self._future = None

    # -- public control ----------------------------------------------------

    def submit(self, text: str) -> None:
        self._queue_guard.clear()
        self._requests.append(text)
        self._queue_guard.set()

    def cancel_current(self) -> None:
        loop = self._loop
        future = self._future
        if loop is not None and future is not None:
            try:
                loop.call_soon_threadsafe(future.cancel)
            except Exception:
                pass

    def shutdown(self) -> None:
        self._stop_flag = True
        self._queue_guard.set()
        self.wait(5000)

    # -- thread body -------------------------------------------------------

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            while not self._stop_flag:
                self._queue_guard.wait(0.5)
                if not self._requests:
                    self._queue_guard.clear()
                    continue
                text = self._requests.pop(0)
                if text is _STOP:
                    break
                try:
                    self._future = loop.create_task(self._do(text))
                    loop.run_until_complete(self._future)
                except asyncio.CancelledError:
                    self.cancelled.emit()
                    log.info("request cancelled by user")
                except Exception as exc:  # noqa: BLE001
                    log.exception("request failed")
                    self.failed.emit(str(exc), "")
                finally:
                    self._future = None
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _do(self, text: str) -> str:
        config = self._config
        system, query = prepare_prompt(config, text)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": query if query else text},
        ]
        chain = provider_chain(config)

        def emit_start(provider: str) -> None:
            self.started.emit(provider)

        async with _build_client() as client:
            full, provider = await run_request(
                config, chain, messages,
                on_chunk=self.chunk.emit,
                on_provider=emit_start,
                client=client,
            )
        self.done.emit(full, provider)
        return full


class AIEngine(QObject):
    """Thread-safe facade the GUI talks to."""

    started = Signal(str)
    chunk = Signal(str)
    done = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, config: ConfigManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = EngineWorker(config)
        self._worker.started.connect(self.started)
        self._worker.chunk.connect(self.chunk)
        self._worker.done.connect(self.done)
        self._worker.failed.connect(self.failed)
        self._worker.cancelled.connect(self.cancelled)

    @property
    def is_running(self) -> bool:
        return self._worker.isRunning()

    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def submit(self, text: str) -> None:
        self.start()
        self._worker.submit(text)

    def cancel(self) -> None:
        self._worker.cancel_current()

    def stop(self) -> None:
        self._worker.shutdown()