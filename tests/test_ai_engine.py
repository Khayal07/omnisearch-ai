"""Verification tests for the unified async streaming engine.

Transport functions are exercised against ``httpx.MockTransport`` so no real
network access is required; the Qt worker thread is tested end-to-end with a
mock transport injected into the client factory.
"""

import json

import httpx
import pytest

from config import ConfigManager
from core import ai_engine
from core.ai_engine import (
    NoProviderAvailableError,
    ProviderError,
    ProviderOfflineError,
    prepare_prompt,
    provider_chain,
    run_request,
    stream_gemini,
    stream_ollama,
    stream_openai,
)

CHUNK_A = "The quick brown fox"
CHUNK_B = " jumps over the lazy dog."


def make_config(tmp_path, provider="openai"):
    cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
    cfg.set("provider", provider)
    cfg.set_api_key("openai", "sk-test")
    cfg.set_api_key("gemini", "gz-test")
    return cfg


def sse_body(*data_lines: str, done: bool = True) -> str:
    lines = [f"data: {line}" for line in data_lines]
    if done:
        lines.append("data: [DONE]")
    return "\n".join(lines) + "\n"


def openai_chunk(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def gemini_chunk(text: str) -> str:
    return json.dumps(
        {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    )


def mock_sse(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _async_client_factory(body: str):
    """Return a factory that builds a *fresh* AsyncClient per call.

    Each worker query opens and closes its own client, so a fresh instance must
    be created for every submission (a shared client would be already-closed by
    the second query).
    """

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=10,
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)),
        )

    return factory


MESSAGES = [
    {"role": "system", "content": "You are OmniSearch."},
    {"role": "user", "content": "hello"},
]


# -- prompt / chain helpers -----------------------------------------------

class TestPromptHelpers:
    def test_default_system_prompt(self, tmp_path):
        cfg = make_config(tmp_path)
        system, query = prepare_prompt(cfg, "explain influenza")
        assert query == "explain influenza"
        assert system == cfg.get("system_prompt")

    def test_quick_action_injected(self, tmp_path):
        cfg = make_config(tmp_path)
        template = cfg.get("quick_actions./sum")
        system, query = prepare_prompt(cfg, "/sum very long text here")
        assert system == template
        assert query == "very long text here"

    def test_quick_action_without_body(self, tmp_path):
        cfg = make_config(tmp_path)
        system, query = prepare_prompt(cfg, "/code")
        assert system == cfg.get("quick_actions./code")
        assert query == ""

    def test_case_insensitive_quick_action(self, tmp_path):
        cfg = make_config(tmp_path)
        system, query = prepare_prompt(cfg, "/SUM body")
        assert system == cfg.get("quick_actions./sum")

    def test_no_quick_action_preserves_text(self, tmp_path):
        cfg = make_config(tmp_path)
        system, query = prepare_prompt(cfg, "/notconfigured still here")
        assert system == cfg.get("system_prompt")
        assert query == "/notconfigured still here"

    def test_provider_chain_active_first(self, tmp_path):
        cfg = make_config(tmp_path, provider="openai")
        assert provider_chain(cfg) == ["openai", "ollama", "gemini"]

    def test_provider_chain_no_duplicates(self, tmp_path):
        cfg = make_config(tmp_path, provider="gemini")
        assert provider_chain(cfg) == ["gemini", "ollama", "openai"]


# -- OpenAI-compatible streaming -----------------------------------------

class TestOpenAIStream:
    @pytest.mark.asyncio
    async def test_streams_chunks(self, tmp_path):
        cfg = make_config(tmp_path)
        handler = lambda req: httpx.Response(
            200, text=sse_body(openai_chunk(CHUNK_A), openai_chunk(CHUNK_B)),
        )
        chunks = []
        async with mock_sse(handler) as client:
            full = await stream_openai(
                cfg, "openai", MESSAGES, chunks.append,
                client=client, model="gpt-4o-mini",
            )
        assert chunks == [CHUNK_A, CHUNK_B]
        assert full == CHUNK_A + CHUNK_B

    @pytest.mark.asyncio
    async def test_auth_error_raises_provider_error(self, tmp_path):
        cfg = make_config(tmp_path)
        handler = lambda req: httpx.Response(401, text="bad key")
        async with mock_sse(handler) as client:
            with pytest.raises(ProviderError) as exc:
                await stream_openai(
                    cfg, "openai", MESSAGES, lambda s: None,
                    client=client, model="gpt-4o-mini",
                )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_api_key_raises(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.set_api_key("openai", None)
        async with mock_sse(lambda req: httpx.Response(200, text="")) as client:
            with pytest.raises(ProviderError):
                await stream_openai(
                    cfg, "openai", MESSAGES, lambda s: None,
                    client=client, model="gpt-4o-mini",
                )

    @pytest.mark.asyncio
    async def test_offline_raises_offline_error(self, tmp_path):
        cfg = make_config(tmp_path)

        def offline(req):
            raise httpx.ConnectError("refused", request=req)

        async with mock_sse(offline) as client:
            with pytest.raises(ProviderOfflineError):
                await stream_openai(
                    cfg, "openai", MESSAGES, lambda s: None,
                    client=client, model="gpt-4o-mini",
                )


# -- Gemini streaming -----------------------------------------------------

class TestGeminiStream:
    @pytest.mark.asyncio
    async def test_streams_parts(self, tmp_path):
        cfg = make_config(tmp_path)
        body = sse_body(gemini_chunk("Hello"), gemini_chunk(" world"))
        async with mock_sse(lambda req: httpx.Response(200, text=body)) as client:
            chunks = []
            full = await stream_gemini(
                cfg, "gemini", MESSAGES, chunks.append,
                client=client, model="gemini-2.0-flash",
            )
        assert chunks == ["Hello", " world"]
        assert full == "Hello world"

    @pytest.mark.asyncio
    async def test_system_instruction_included(self, tmp_path):
        cfg = make_config(tmp_path)
        captured = {}

        def handler(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, text=sse_body(gemini_chunk("ok")))

        async with mock_sse(handler) as client:
            await stream_gemini(
                cfg, "gemini", MESSAGES, lambda s: None,
                client=client, model="gemini-2.0-flash",
            )
        assert captured["body"]["systemInstruction"]["parts"][0]["text"] == MESSAGES[0]["content"]

    @pytest.mark.asyncio
    async def test_no_key_raises(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.set_api_key("gemini", None)
        async with mock_sse(lambda req: httpx.Response(200, text="")) as client:
            with pytest.raises(ProviderError):
                await stream_gemini(
                    cfg, "gemini", MESSAGES, lambda s: None,
                    client=client, model="gemini-2.0-flash",
                )


# -- Ollama streaming -----------------------------------------------------

class TestOllamaStream:
    @pytest.mark.asyncio
    async def test_streams_ndjson(self, tmp_path):
        cfg = make_config(tmp_path)
        lines = [
            json.dumps({"message": {"role": "assistant", "content": "one"}}),
            json.dumps({"message": {"role": "assistant", "content": " two"}}),
            json.dumps({"done": True}),
        ]
        async with mock_sse(lambda req: httpx.Response(200, text="\n".join(lines) + "\n")) as client:
            chunks = []
            full = await stream_ollama(
                cfg, "ollama", MESSAGES, chunks.append,
                client=client, model="qwen2.5:7b",
            )
        assert chunks == ["one", " two"]
        assert full == "one two"


# -- fallback chain -------------------------------------------------------

class TestFallback:
    @pytest.mark.asyncio
    async def test_falls_back_on_offline(self, tmp_path):
        cfg = make_config(tmp_path)
        seen_providers = []

        def offline(req):
            raise httpx.ConnectError("server down", request=req)

        def ok(req):
            return httpx.Response(200, text=sse_body(openai_chunk("recovered")))

        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: offline(req) if "localhost" in str(req.url) else ok(req)
        ))
        async with client:
            full, provider = await run_request(
                cfg, ["ollama", "openai"], MESSAGES,
                on_chunk=lambda s: None,
                on_provider=seen_providers.append,
                client=client,
            )
        assert provider == "openai"
        assert full == "recovered"
        assert seen_providers == ["ollama", "openai"]

    @pytest.mark.asyncio
    async def test_all_offline_raises(self, tmp_path):
        cfg = make_config(tmp_path)

        def offline(req):
            raise httpx.ConnectError("down", request=req)

        async with mock_sse(offline) as client:
            with pytest.raises(NoProviderAvailableError):
                await run_request(
                    cfg, ["ollama", "openai"], MESSAGES,
                    on_chunk=lambda s: None,
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_auth_error_does_not_fallback(self, tmp_path):
        cfg = make_config(tmp_path)

        def auth_failure(req):
            return httpx.Response(401, text="denied")

        def ok(req):
            return httpx.Response(200, text=sse_body(openai_chunk("gemini ok")))

        # openai is first and returns 401 -> must surface, not fall through.
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: auth_failure(req)
        ))
        async with client:
            with pytest.raises(ProviderError):
                await run_request(
                    cfg, ["openai", "gemini"], MESSAGES,
                    on_chunk=lambda s: None,
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_chain_skips_provider_without_key(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.set_api_key("openai", None)
        seen_providers = []

        def gemini_responds(req):
            return httpx.Response(200, text=sse_body(gemini_chunk("from gemini")))

        async with mock_sse(gemini_responds) as client:
            full, provider = await run_request(
                cfg, ["openai", "gemini"], MESSAGES,
                on_chunk=lambda s: None,
                on_provider=seen_providers.append,
                client=client,
            )
        assert provider == "gemini"
        assert full == "from gemini"
        assert seen_providers == ["gemini"]  # openai never attempted


# -- Qt worker thread -----------------------------------------------------

class TestEngineWorker:
    def test_end_to_end_stream(self, tmp_path, qapp, qtbot, mocker):
        from core.ai_engine import AIEngine

        cfg = make_config(tmp_path)
        cfg.set("provider", "openai")

        mocker.patch.object(
            ai_engine,
            "_build_client",
            side_effect=_async_client_factory(
                sse_body(openai_chunk("Hi "), openai_chunk("there"))
            ),
        )

        engine = AIEngine(cfg)
        received = []
        provider_calls = []
        results = []

        engine.chunk.connect(received.append)
        engine.started.connect(provider_calls.append)
        engine.done.connect(lambda full, provider: results.append((full, provider)))

        try:
            engine.submit("hello")
            qtbot.waitUntil(lambda: len(results) == 1, timeout=5000)
        finally:
            engine.stop()

        full, provider = results[0]
        assert full == "Hi there"
        assert provider == "openai"
        assert "".join(received) == "Hi there"
        assert provider_calls == ["openai"]

    def test_each_query_gets_a_fresh_worker(self, tmp_path, qapp, qtbot, mocker):
        """Second and subsequent queries must use a brand-new worker thread,
        never a restarted/retired one."""
        from core.ai_engine import AIEngine, EngineWorker

        cfg = make_config(tmp_path)
        cfg.set("provider", "openai")

        mocker.patch.object(
            ai_engine,
            "_build_client",
            side_effect=_async_client_factory(
                sse_body(openai_chunk("Hi "), openai_chunk("there"))
            ),
        )

        engine = AIEngine(cfg)
        results = []
        engine.done.connect(lambda full, provider: results.append(full))

        spawned = []

        original = EngineWorker.__init__

        def tracking_init(self, config, request_text, parent=None):
            spawned.append(request_text)
            original(self, config, request_text, parent)

        mocker.patch.object(EngineWorker, "__init__", tracking_init)

        try:
            engine.submit("query-one")
            qtbot.waitUntil(lambda: len(results) == 1, timeout=5000)
            engine.submit("query-two")
            qtbot.waitUntil(lambda: len(results) == 2, timeout=5000)
            engine.submit("query-three")
            qtbot.waitUntil(lambda: len(results) == 3, timeout=5000)
        finally:
            engine.stop()

        assert results == ["Hi there", "Hi there", "Hi there"]
        # One brand-new EngineWorker per submission.
        assert spawned == ["query-one", "query-two", "query-three"]

    def test_submit_while_busy_is_ignored(self, tmp_path, qapp, qtbot, mocker):
        from core.ai_engine import AIEngine

        cfg = make_config(tmp_path)
        cfg.set("provider", "openai")

        body = sse_body(openai_chunk("slow "))
        mocker.patch.object(
            ai_engine,
            "_build_client",
            side_effect=_async_client_factory(body),
        )

        engine = AIEngine(cfg)
        results = []
        engine.done.connect(lambda full, provider: results.append(full))

        try:
            engine.submit("first")
            # Immediately submitting again while the first is in flight must
            # not queue a second request or spawn a second worker.
            engine.submit("second")
            qtbot.waitUntil(lambda: len(results) == 1, timeout=5000)
            qtbot.wait(100)
            assert len(results) == 1
        finally:
            engine.stop()