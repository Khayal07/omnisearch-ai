"""End-to-end integration tests: overlay <-> engine <-> history with a
mocked transport, plus single-instance lock verification.

These exercise the full wiring without touching the network.
"""

import asyncio
import json

import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from config import ConfigManager
from core import ai_engine
from ui.history import HistoryStore
from ui.overlay import Overlay


def _cfg(tmp_path):
    cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
    cfg.set("provider", "openai")
    cfg.set_api_key("openai", "sk-e2e")
    return cfg


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0.05):
        self._chunks = chunks
        self._delay = delay
        self._closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            await asyncio.sleep(self._delay)
            yield chunk.encode()
        self._closed = True

    async def aclose(self):
        self._closed = True


@pytest.fixture
def overlay(qtbot):
    win = Overlay()
    qtbot.addWidget(win)
    return win


def _stream_openai_line(content: str) -> str:
    delta = json.dumps({"choices": [{"delta": {"content": content}}]})
    return f"data: {delta}\n"


class TestE2EStream:
    def test_two_queries_in_a_row_both_render(self, qtbot, tmp_path, mocker):
        """Regression: subsequent queries must stream tokens again (fresh worker
        per query, response panel cleared between cycles)."""
        body = "".join(
            [_stream_openai_line("Hello "), _stream_openai_line("world"), "data: [DONE]\n"]
        )

        def client_factory():
            return httpx.AsyncClient(
                timeout=10,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, text=body)
                ),
            )

        mocker.patch.object(ai_engine, "_build_client", side_effect=client_factory)

        from core.ai_engine import AIEngine

        hist = HistoryStore(tmp_path / "twoq.db")
        cfg = _cfg(tmp_path)
        engine = AIEngine(cfg)
        win = Overlay()
        win.set_history(hist)
        win.attach_engine(engine)
        qtbot.addWidget(win)
        win.show()

        done_count = []
        engine.done.connect(lambda full, provider: done_count.append(full))

        try:
            win.search.setText("question one")
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)
            assert "Hello world" in win.output.toPlainText()

            win.search.setText("question two")
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)
            assert "Hello world" in win.output.toPlainText()

            assert len(done_count) == 2
            # Consecutive queries CONTINUE the same conversation.
            convs = hist.list_conversations()
            assert len(convs) == 1
            msgs = hist.conversation_messages(convs[0]["id"])
            assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
            assert msgs[0]["content"] == "question one"
            assert msgs[-1]["content"] == "Hello world"
            win.dismiss()
        finally:
            engine.stop()
            hist.close()

    def test_new_chat_opens_a_fresh_conversation(self, qtbot, tmp_path, mocker):
        body = "".join(
            [_stream_openai_line("Hello "), _stream_openai_line("world"), "data: [DONE]\n"]
        )

        def client_factory():
            return httpx.AsyncClient(
                timeout=10,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, text=body)
                ),
            )

        mocker.patch.object(ai_engine, "_build_client", side_effect=client_factory)

        from core.ai_engine import AIEngine

        hist = HistoryStore(tmp_path / "newc.db")
        cfg = _cfg(tmp_path)
        engine = AIEngine(cfg)
        win = Overlay()
        win.set_history(hist)
        win.attach_engine(engine)
        qtbot.addWidget(win)
        win.show()

        try:
            win.search.setText("query one")
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)
            assert len(hist.list_conversations()) == 1

            win.start_new_chat()
            assert win._active_conversation is None
            assert not win.new_chat_btn.isVisible()

            win.search.setText("query two")
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)

            convs = hist.list_conversations()
            assert len(convs) == 2
            assert convs[0]["title"] == "query two"
            assert convs[1]["title"] == "query one"
            win.dismiss()
        finally:
            engine.stop()
            hist.close()

    def test_continue_chat_sends_prior_context(self, qtbot, tmp_path, mocker):
        body = "".join(
            [_stream_openai_line("Hello "), _stream_openai_line("world"), "data: [DONE]\n"]
        )
        bodies = []

        def client_factory():
            def handler(req):
                bodies.append(json.loads(req.content))
                return httpx.Response(200, text=body)

            return httpx.AsyncClient(
                timeout=10,
                transport=httpx.MockTransport(handler),
            )

        mocker.patch.object(ai_engine, "_build_client", side_effect=client_factory)

        from core.ai_engine import AIEngine

        hist = HistoryStore(tmp_path / "cont.db")
        cfg = _cfg(tmp_path)
        engine = AIEngine(cfg)
        win = Overlay()
        win.set_history(hist)
        win.attach_engine(engine)
        qtbot.addWidget(win)
        win.show()

        try:
            win.search.setText("first question")
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)

            convs = hist.list_conversations()
            assert len(convs) == 1
            chat_id = convs[0]["id"]

            win._open_conversation(chat_id)
            win.search.setText("second question")
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)

            # Continued the SAME chat (no new conversation created).
            assert len(hist.list_conversations()) == 1
            msgs = hist.conversation_messages(chat_id)
            assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
            assert msgs[0]["content"] == "first question"
            assert msgs[1]["content"] == "Hello world"

            # The 2nd provider request carried the earlier turns as context.
            second = bodies[1]["messages"]
            assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
            assert second[-1]["content"] == "second question"
            assert second[-2]["content"] == "Hello world"

            assert "Hello world" in win.output.toPlainText()
            assert "first question" in win.output.toPlainText()
            win.dismiss()
        finally:
            engine.stop()
            hist.close()

    def test_submit_streams_into_overlay_and_records_history(
        self, qtbot, tmp_path, mocker
    ):
        body = "".join(
            [_stream_openai_line("Hello "), _stream_openai_line("world"), "data: [DONE]\n"]
        )
        mocker.patch.object(
            ai_engine,
            "_build_client",
            return_value=httpx.AsyncClient(
                timeout=10,
                transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)),
            ),
        )

        win = Overlay()
        hist = HistoryStore(tmp_path / "e2e.db")
        engine = ai_engine.AIEngine(_cfg(tmp_path))
        win.set_history(hist)
        win.attach_engine(engine)
        qtbot.addWidget(win)

        win.show()
        win.search.setText("summarize X")

        try:
            with qtbot.waitSignal(engine.done, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)
            assert "Hello world" in win.output.toPlainText()
            convs = hist.list_conversations()
            assert len(convs) == 1
            assert convs[0]["title"] == "summarize X"
            msgs = hist.conversation_messages(convs[0]["id"])
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert msgs[0]["content"] == "summarize X"
            assert msgs[1]["content"] == "Hello world"
            win.dismiss()
        finally:
            engine.stop()
            hist.close()

    def test_escape_cancels_in_flight_stream(self, qtbot, tmp_path, mocker):
        chunks = [_stream_openai_line("x") for _ in range(200)]  # no [DONE] -> never finishes
        mocker.patch.object(
            ai_engine,
            "_build_client",
            return_value=httpx.AsyncClient(
                timeout=10,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200, stream=SlowStream(chunks, delay=0.02)
                    )
                ),
            ),
        )

        cfg = _cfg(tmp_path)
        hist = HistoryStore(tmp_path / "e2ec.db")
        engine = ai_engine.AIEngine(cfg)
        win = Overlay()
        win.set_history(hist)
        win.attach_engine(engine)
        qtbot.addWidget(win)

        win.show()
        win.search.setText("count forever")

        try:
            with qtbot.waitSignal(engine.started, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Return)
            assert win._busy is True

            with qtbot.waitSignal(engine.cancelled, timeout=5000):
                QTest.keyClick(win.search, Qt.Key_Escape)

            qtbot.waitUntil(lambda: win._busy is False, timeout=2000)
            assert not win._busy
            # The user turn was recorded, but a cancelled request stores no answer.
            convs = hist.list_conversations()
            assert convs and convs[0]["title"] == "count forever"
            msgs = hist.conversation_messages(convs[0]["id"])
            assert [m["role"] for m in msgs] == ["user"]
        finally:
            engine.stop()
            hist.close()


class TestSingleInstance:
    @pytest.mark.skipif(__import__("sys").platform != "win32",
                        reason="named mutex is Windows-only")
    def test_second_instance_is_blocked(self):
        from core.single_instance import SingleInstanceLock

        first = SingleInstanceLock("e2e-app")
        assert first.is_primary

        second = SingleInstanceLock("e2e-app")
        assert not second.is_primary

        first.close()
        third = SingleInstanceLock("e2e-app")
        assert third.is_primary
        third.close()