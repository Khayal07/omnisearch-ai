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
import re
from typing import Awaitable, Callable, Iterable

import httpx
from PySide6.QtCore import QObject, QThread, Signal

from config import ConfigManager
from core.tool_caller import ToolCaller

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
# Local tool definitions (Function Calling)
# ---------------------------------------------------------------------------

LOCAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search local files on this Windows PC by file name. Returns the "
                "filename, full path, file type and last-modified time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "File-name substring to match"},
                    "extension": {"type": "string", "description": "Optional extension filter, e.g. 'pdf'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_app",
            "description": "Launch an installed application by name (from the Windows Start Menu).",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "Application name, e.g. 'Visual Studio Code'"},
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_content",
            "description": (
                "Read the text content of a local file (.txt, .md, .py, .json, .csv, "
                ".log, .pdf, .docx, ...). Returns a truncated excerpt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Full path to the local file"},
                },
                "required": ["file_path"],
            },
        },
    },
]

_INTENT_VERBS = {
    "launch": r"^(?:please\s+)?(?:launch|start|run|open)\s+(?:the\s+)?(?:app\s+|application\s+|program\s+)?(.+?)\s*$",
    "search": r"\b(?:find|search|locate|looking\s+for|get\s+me|where\s+(?:is|are))\b",
    "read": r"\b(?:read|open|show|view|summarize|summarise)\s+(?:the\s+)?(?:contents?\s+of\s+|file\s+)?([^\s,;]+(?:\.[a-z0-9]{1,8}))\b",
}
_EXTENSIONS = {
    "pdf", "docx", "doc", "txt", "md", "markdown", "py", "json", "csv",
    "log", "xlsx", "xls", "png", "jpg", "jpeg", "zip", "html", "xml",
    "sql", "yml", "yaml", "ppt", "pptx",
}
_EXT_PATTERN = re.compile(r"\.(pdf|docx|doc|txt|md|py|json|csv|log|xlsx|xls|png|jpg|jpeg|zip|html|xml|sql|yml|yaml|ppt|pptx)\b", re.I)
_EXT_WORD = re.compile(r"\b(?:pdf|docx|doc|txt|md|py|json|csv|log|xlsx|xls|png|jpg|jpeg|zip|html)\b", re.I)
_SEARCH_STOP = re.compile(
    r"\b(?:find|search|locate|looking|for|my|a|an|the|me|files|file|documents|"
    r"document|folder|folders|of|on|in|into|and|with|please|all|any|that|need|"
    r"to|downloaded|drive|disk|computer|system|summarize|summarise|about|show|"
    r"it|them|its|their)\b",
    re.I,
)
_TOOL_SIGNALS = re.compile(
    r"\b(?:find|search|locate|launch|start|run|open|read|show|view|"
    r"file|files|document|documents|folder|folders|app|application|"
    r"program|install|resume|budget|report|archive)\b",
    re.I,
)


def _extract_extension(text: str) -> str | None:
    match = _EXT_PATTERN.search(text)
    if match:
        return match.group(1).lower()
    word = _EXT_WORD.search(text)
    if word:
        return word.group(0).lower() if word.group(0).lower() in _EXTENSIONS else None
    return None


def detect_tool_intent(text: str) -> list[dict]:
    """Deterministic, offline decoder of clear local-tool requests.

    Returns a list of ``{"name": tool, "arguments": {...}}`` actions. The model
    keeps the final say for ambiguous queries via the OpenAI tool round; this
    decoder only fires on unmistakable patterns so ordinary chat stays intact.
    """
    low = (text or "").strip()
    if not low:
        return []

    read = re.search(_INTENT_VERBS["read"], low, re.I)
    if read and (":" in read.group(1) or read.group(1).count(".")):
        return [{"name": "read_file_content", "arguments": {"file_path": read.group(1)}}]

    launch = re.match(_INTENT_VERBS["launch"], low, re.I)
    if launch:
        app = launch.group(1).strip().strip(".")
        app = re.sub(r"\s+(?:now|please)$", "", app, flags=re.I).strip()
        if app:
            return [{"name": "launch_app", "arguments": {"app_name": app}}]

    if re.search(_INTENT_VERBS["search"], low, re.I):
        extension = _extract_extension(low)
        has_file_word = re.search(r"\b(?:file|files|document|documents|folder|folders)\b", low, re.I)
        if extension is not None or has_file_word:
            cleaned = re.sub(r"\.(?:pdf|docx|doc|txt|md|py|json|csv|log)\b", " ", low, flags=re.I)
            cleaned = _SEARCH_STOP.sub(" ", cleaned)
            words = [
                w
                for w in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_\-.]*", cleaned)
                if len(w) > 1 and w.lower() not in _EXTENSIONS
            ]
            query = " ".join(words[:5]).strip()
            arguments: dict = {"query": query or low.strip()}
            if extension is not None:
                arguments["extension"] = extension
            return [{"name": "search_files", "arguments": arguments}]

    return []


def _has_tool_signal(text: str) -> bool:
    """Broad gate: does the query even look like it could touch a local tool?"""
    return bool(text and _TOOL_SIGNALS.search(text))


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
            # Providers that require a key are skipped (not errored) when the
            # key is missing, so a configured provider further down the chain
            # still gets a chance to answer.
            if name in ("openai", "gemini") and not config.api_key(name):
                log.info("skipping provider %r: no API key configured", name)
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


async def request_openai_tool_calls(
    config: ConfigManager,
    provider: str,
    messages: list[dict],
    *,
    client: httpx.AsyncClient,
    model: str,
) -> tuple[str, list[dict]]:
    """Non-streaming OpenAI round that asks the model to choose a local tool.

    Returns ``(assistant_content, tool_calls)``. Content alone (no tool calls)
    means the model answered directly. Any HTTP/JSON failure raises
    ``ProviderError`` so the caller can fall back to normal streaming.
    """
    key = config.api_key(provider)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = _openai_url(config, provider)
    payload = _openai_body(config, provider, model, messages)
    payload["stream"] = False
    payload["tools"] = LOCAL_TOOLS
    payload["tool_choice"] = "auto"
    try:
        response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderOfflineError(provider, f"Connection failed: {exc}") from exc
    if response.status_code >= 400:
        raise ProviderError(
            provider,
            f"API error {response.status_code}: {response.text[:200]}",
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError(provider, "Empty or malformed tool-decision response") from exc
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    return (message.get("content") or ""), list(tool_calls)


# ---------------------------------------------------------------------------
# Qt worker thread
# ---------------------------------------------------------------------------

class EngineWorker(QThread):
    """One-shot worker: handles exactly one request, then finishes.

    A *fresh* instance is created for every query so the thread and its
    asyncio event loop are never reused across requests. The AIEngine reaps
    each finished worker with ``finished -> deleteLater()``.
    """

    started = Signal(str)
    chunk = Signal(str)
    done = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(
        self,
        config: ConfigManager,
        request_text: str,
        context: list[dict] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._request = request_text
        self._context = context
        self.tool_caller: ToolCaller | None = None
        self._loop = None
        self._future = None

    # -- public control ----------------------------------------------------

    def cancel_current(self) -> None:
        loop = self._loop
        future = self._future
        if loop is not None and future is not None:
            try:
                loop.call_soon_threadsafe(future.cancel)
            except Exception:
                pass

    # -- thread body -------------------------------------------------------

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            self._future = loop.create_task(self._stream())
            try:
                loop.run_until_complete(self._future)
            except asyncio.CancelledError:
                log.info("request cancelled by user")
                self.cancelled.emit()
            except Exception as exc:  # noqa: BLE001
                log.exception("request failed")
                self.failed.emit(str(exc), "")
        finally:
            self._future = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _stream(self) -> None:
        config = self._config
        system, query = prepare_prompt(config, self._request)
        messages: list[dict] = [{"role": "system", "content": system}]
        if self._context:
            messages.extend(self._context)
        final_user = query if query else self._request
        messages.append({"role": "user", "content": final_user})

        tool_caller = self.tool_caller
        if config.get("tool_calling", True) and tool_caller is not None:
            actions = detect_tool_intent(final_user)
            if actions:
                # Deterministic local pre-pass: run tools, let the model
                # render an answer grounded in the tool results.
                summaries = [
                    f"{action['name']}: {tool_caller.execute(action['name'], action.get('arguments') or {})}"
                    for action in actions
                ]
                messages.append(
                    {
                        "role": "user",
                        "content": "[Local tool results]\n" + "\n".join(summaries),
                    }
                )
            elif _has_tool_signal(final_user):
                # Ambiguous tool-ish query: let an OpenAI-compatible model
                # decide via Function Calling before we stream the answer.
                full = await self._maybe_model_tool_round(messages)
                if full is not None:
                    return

        chain = provider_chain(config)
        async with _build_client() as client:
            full, provider = await run_request(
                config, chain, messages,
                on_chunk=self.chunk.emit,
                on_provider=self.started.emit,
                client=client,
            )
        self.done.emit(full, provider)

    async def _maybe_model_tool_round(self, messages: list[dict]) -> str | None:
        """Ask an OpenAI-compatible provider for a tool decision.

        Executes any requested tool and streams the final, tool-grounded
        answer. Returns ``None`` to signal "fall back to the normal chain".
        """
        config = self._config
        tool_caller = self.tool_caller
        if tool_caller is None:
            return None
        for name in provider_chain(config):
            if name in ("gemini", "ollama"):
                continue
            pc = config.provider_config(name)
            model = pc.get("model") or config.get("model") or ""
            if not model:
                continue
            if name == "openai" and not config.api_key(name):
                continue
            if name not in ("openai", "custom") and not config.api_key(name):
                continue
            try:
                async with _build_client() as client:
                    content, tool_calls = await request_openai_tool_calls(
                        config, name, messages, client=client, model=model
                    )
            except (ProviderError, httpx.HTTPError, ValueError):
                log.debug("tool-decision round failed on %r; falling back", name)
                continue
            if not tool_calls:
                if not content:
                    return None
                self.chunk.emit(content)
                self.done.emit(content, name)
                return "done"
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                }
            )
            messages.extend(tool_caller.dispatch(tool_calls))
            async with _build_client() as client:
                full, provider = await run_request(
                    config, [name], messages,
                    on_chunk=self.chunk.emit,
                    on_provider=self.started.emit,
                    client=client,
                )
            self.done.emit(full, provider)
            return "done"
        return None


class AIEngine(QObject):
    """Frames each query with a brand-new EngineWorker thread.

    Signals started / chunk / done / failed / cancelled are forwarded straight
    through to the GUI. A worker that has finished is dropped and scheduled
    for deletion (``finished -> deleteLater()``), never restarted.
    """

    started = Signal(str)
    chunk = Signal(str)
    done = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, config: ConfigManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._worker: EngineWorker | None = None
        self._tool_caller = ToolCaller(config.apps_db_path, config.files_db_path)

    @property
    def is_running(self) -> bool:
        worker = self._worker
        return worker is not None and worker.isRunning()

    def submit(self, text: str, context: list[dict] | None = None) -> None:
        """Launch a fresh worker thread for `text`. Ignores queuing while busy.

        `context` is an optional list of prior messages (``role``/``content``)
        prepended to the request so the provider can see the conversation.
        """
        if self.is_running:
            log.debug("ignoring submit while a request is already in flight")
            return
        worker = EngineWorker(self._config, text, context)
        worker.tool_caller = self._tool_caller
        worker.started.connect(self.started)
        worker.chunk.connect(self.chunk)
        worker.done.connect(self.done)
        worker.failed.connect(self.failed)
        worker.cancelled.connect(self.cancelled)
        worker.finished.connect(self._on_worker_finished)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        log.info("starting worker thread for new query")
        worker.start()

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not None and worker is self._worker:
            self._worker = None

    def cancel(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel_current()

    def stop(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel_current()
            worker.wait(3000)