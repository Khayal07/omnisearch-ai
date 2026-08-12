"""Verification tests for local system features: Start Menu app indexing,
fast file search, file content reading and AI tool dispatch.
"""

import json
import struct
import time
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest

from config import ConfigManager
from core import ai_engine
from core.ai_engine import detect_tool_intent
from core.app_indexer import AppIndexer, _resolve_lnk_target
from core.file_reader import FileContentReader, truncate
from core.file_search import FileSearch
from core.tool_caller import ToolCaller


def make_config(tmp_path, provider="openai"):
    cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
    cfg.set("provider", provider)
    cfg.set("tool_calling", True)
    cfg.set_api_key("openai", "sk-test")
    return cfg


def sse_body(*data_lines: str) -> str:
    lines = [f"data: {line}" for line in data_lines]
    lines.append("data: [DONE]")
    return "\n".join(lines) + "\n"


def openai_chunk(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": text}}]})


# ---------------------------------------------------------------------------
# App indexer / launcher
# ---------------------------------------------------------------------------

class TestAppIndexer:
    def test_scan_fake_shortcuts_does_not_crash(self, tmp_path):
        start_menu = tmp_path / "Programs"
        start_menu.mkdir(parents=True)
        (start_menu / "Broken.lnk").write_bytes(b"\x00" * 200)
        (start_menu / "Note.txt").write_text("not a shortcut")
        sub = start_menu / "Games"
        sub.mkdir()
        (sub / "Mystery.lnk").write_bytes(b"junk")

        indexer = AppIndexer(tmp_path / "apps.db", start_menu_dirs=[start_menu])
        try:
            added = indexer.scan()
        finally:
            indexer.close()
        assert added == 2  # only the two .lnk files are indexed

    def test_scan_rescans_only_on_force(self, tmp_path):
        start_menu = tmp_path / "Programs"
        start_menu.mkdir(parents=True)
        (start_menu / "Code.lnk").write_bytes(b"junk")

        indexer = AppIndexer(tmp_path / "apps.db", start_menu_dirs=[start_menu])
        try:
            assert indexer.scan() == 1
            assert indexer.scan() == 0  # cache already warm
            assert indexer.scan(force=True) >= 1
        finally:
            indexer.close()

    def test_add_search_and_find(self, tmp_path):
        indexer = AppIndexer(tmp_path / "apps.db" , start_menu_dirs=[])
        try:
            indexer.add_app("Visual Studio Code", "C:/tools/vscode/Code.exe")
            indexer.add_app("Notepad", "C:/Windows/System32/notepad.exe")
            hits = indexer.search("visual")
            assert hits[0]["name"] == "Visual Studio Code"
            found = indexer.find_app("notepad")
            assert found is not None and found["path"] == "C:/Windows/System32/notepad.exe"
            assert indexer.find_app("zzz-no-such-app") is None
        finally:
            indexer.close()

    def test_launch_app_picks_correct_path(self, tmp_path, mocker):
        startfile = mocker.patch("core.app_indexer.os.startfile")
        indexer = AppIndexer(tmp_path / "apps.db", start_menu_dirs=[])
        try:
            indexer.add_app("Notepad", "C:/Windows/System32/notepad.exe")
        finally:
            indexer.close()
        indexer2 = AppIndexer(tmp_path / "apps.db", start_menu_dirs=[])
        try:
            result = json.loads(indexer2.launch_app("notepad"))
            assert result["ok"] is True
            startfile.assert_called_once_with("C:/Windows/System32/notepad.exe")
        finally:
            indexer2.close()

    def test_launch_app_unknown_returns_suggestions(self, tmp_path, mocker):
        startfile = mocker.patch("core.app_indexer.os.startfile")
        indexer = AppIndexer(tmp_path / "apps.db", start_menu_dirs=[])
        try:
            indexer.add_app("Visual Studio Code", "C:/VSCode/Code.exe")
        finally:
            indexer.close()
        indexer2 = AppIndexer(tmp_path / "apps.db", start_menu_dirs=[])
        try:
            # fuzzy match resolves to the closest installed app
            result = json.loads(indexer2.launch_app("visual studio"))
            assert result["ok"] is True
            assert result["path"] == "C:/VSCode/Code.exe"
            startfile.assert_called_once_with("C:/VSCode/Code.exe")
            # a truly unknown app returns suggestions and matches nothing
            startfile.reset_mock()
            miss = json.loads(indexer2.launch_app("some visual thing"))
            assert miss["ok"] is False
            assert miss["suggestions"] == ["Visual Studio Code"]
            startfile.assert_not_called()
        finally:
            indexer2.close()

    def test_resolve_lnk_target(self):
        def build_lnk(target):
            header = (
                b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00"
                b"\xc0\x00\x00\x00\x00\x00\x00\x46" + b"\x00" * 56
            )
            path_bytes = target.encode("cp1252") + b"\x00"
            local_base = 0x1C
            common_suffix = 0x1C + len(path_bytes)
            link_info = struct.pack("<7I", 0x1C, 0x1C, 0, 0, local_base, 0, common_suffix)
            return header + link_info + path_bytes

        target = "C:\\Program Files\\Example\\app.exe"
        assert _resolve_lnk_target(build_lnk(target)) == target
        assert _resolve_lnk_target(b"not a lnk at all") is None


# ---------------------------------------------------------------------------
# Fast file search
# ---------------------------------------------------------------------------

class TestFileSearch:
    def test_search_returns_expected_fields(self, tmp_path):
        fs = FileSearch(tmp_path / "files.db")
        try:
            fs.add_index_rows(
                [
                    {"name": "budget.xlsx", "path": "C:/docs/budget.xlsx", "ext": "xlsx", "modified": 1700000000},
                    {"name": "notes.txt", "path": "C:/docs/notes.txt", "ext": "txt", "modified": 1700000001},
                ]
            )
            hits = fs.search("budget")
            assert hits and set(hits[0]) == {"name", "path", "type", "modified"}
            assert hits[0]["name"] == "budget.xlsx"
            assert hits[0]["path"] == "C:/docs/budget.xlsx"
        finally:
            fs.close()

    def test_extension_filter(self, tmp_path):
        fs = FileSearch(tmp_path / "files.db")
        try:
            fs.add_index_rows(
                [
                    {"name": "report.pdf", "path": "C:/docs/report.pdf", "ext": "pdf", "modified": 0},
                    {"name": "report.txt", "path": "C:/docs/report.txt", "ext": "txt", "modified": 0},
                ]
            )
            hits = fs.search("report", extension="pdf")
            assert [h["name"] for h in hits] == ["report.pdf"]
        finally:
            fs.close()

    def test_search_case_insensitive(self, tmp_path):
        fs = FileSearch(tmp_path / "files.db")
        try:
            fs.add_index_rows([{"name": "TAX-Return-2026.pdf", "path": "C:/t.pdf", "ext": "pdf", "modified": 0}])
            hits = fs.search("tax-return")
            assert len(hits) == 1
        finally:
            fs.close()

    def test_query_execution_under_100ms(self, tmp_path):
        fs = FileSearch(tmp_path / "files.db")
        try:
            rows = [
                {"name": f"report_{i:05d}.txt", "path": f"C:/many/report_{i:05d}.txt", "ext": "txt", "modified": 1000}
                for i in range(20_000)
            ]
            fs.add_index_rows(rows)
            start = time.perf_counter()
            hits = fs.search("report_10042")
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert hits and hits[0]["name"] == "report_10042.txt"
            assert elapsed_ms < 100, f"search took {elapsed_ms:.1f}ms"
        finally:
            fs.close()


# ---------------------------------------------------------------------------
# File content reader
# ---------------------------------------------------------------------------

def make_docx(path: Path) -> None:
    xml = (
        b'<?xml version="1.0"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body>"
        b"<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> world</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>"
        b"</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
        )
        z.writestr("word/document.xml", xml)


def make_pdf(path: Path) -> None:
    data = zlib.compress(b"BT /F1 12 Tf 72 700 Td (Hello World PDF) Tj ET")
    pdf = path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Length " + str(len(data)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + data + b"\nendstream\nendobj\n"
    )
    assert pdf


class TestFileContentReader:
    def test_reads_text_file(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_text("# Title\nBody text", encoding="utf-8")
        result = FileContentReader().read(path)
        assert result["ok"] is True
        assert result["kind"] == "text"
        assert result["content"].replace("\r\n", "\n") == "# Title\nBody text"

    def test_reads_file_missing(self, tmp_path):
        result = FileContentReader().read(tmp_path / "nope.xyz")
        assert result["ok"] is False

    def test_truncates_long_content(self, tmp_path):
        path = tmp_path / "big.txt"
        path.write_text("word " * 500)
        result = FileContentReader().read(path, max_chars=100)
        assert result["truncated"] is True
        assert len(result["content"]) <= 100
        assert result["content"].endswith("[truncated]")

        clipped, was = truncate("hi there world", 6)
        assert was is True and clipped.startswith("\n…[truncated]") is False
        assert len(clipped) <= 6 + len("\n…[truncated]")

    def test_reads_docx(self, tmp_path):
        path = tmp_path / "memo.docx"
        make_docx(path)
        result = FileContentReader().read(path)
        assert result["ok"] is True
        assert result["kind"] == "docx"
        assert "Hello world" in result["content"]
        assert "Second paragraph" in result["content"]

    def test_reads_pdf(self, tmp_path):
        path = tmp_path / "resume.pdf"
        make_pdf(path)
        result = FileContentReader().read(path)
        assert result["ok"] is True
        assert result["kind"] == "pdf"
        assert "Hello World PDF" in result["content"]


# ---------------------------------------------------------------------------
# Tool intent decoding
# ---------------------------------------------------------------------------

class TestDetectToolIntent:
    def test_launch_app_pattern(self):
        actions = detect_tool_intent("Launch Visual Studio Code")
        assert actions == [{"name": "launch_app", "arguments": {"app_name": "Visual Studio Code"}}]

    def test_please_open_terminal(self):
        actions = detect_tool_intent("please open terminal")
        assert actions[0]["name"] == "launch_app"
        assert actions[0]["arguments"]["app_name"] == "terminal"

    def test_find_file_with_extension(self):
        actions = detect_tool_intent("Find my resume pdf and summarize it")
        assert actions[0]["name"] == "search_files"
        assert "resume" in actions[0]["arguments"]["query"]
        assert actions[0]["arguments"]["extension"] == "pdf"

    def test_search_for_files(self):
        actions = detect_tool_intent("search for python files")
        assert actions[0]["name"] == "search_files"
        assert "python" in actions[0]["arguments"]["query"]
        assert "extension" not in actions[0]["arguments"]

    def test_read_file_path(self):
        actions = detect_tool_intent("read C:\\Users\\me\\notes.md")
        assert actions[0]["name"] == "read_file_content"
        assert actions[0]["arguments"]["file_path"] == "C:\\Users\\me\\notes.md"

    def test_summarize_file_content(self):
        actions = detect_tool_intent("summarize the contents of C:\\tmp\\report.pdf")
        assert actions[0]["name"] == "read_file_content"
        assert actions[0]["arguments"]["file_path"] == "C:\\tmp\\report.pdf"

    def test_plain_chat_has_no_intent(self):
        assert detect_tool_intent("What is the capital of France?") == []
        assert detect_tool_intent("hello there") == []


# ---------------------------------------------------------------------------
# Tool caller dispatch
# ---------------------------------------------------------------------------

class TestToolCaller:
    def test_execute_search_files(self, tmp_path, mocker):
        mocker.patch.object(
            FileSearch, "search",
            return_value=[{"name": "budget.pdf", "path": "C:/docs/budget.pdf", "type": "PDF", "modified": "2026-01-01"}],
        )
        caller = ToolCaller(tmp_path / "apps.db", tmp_path / "files.db")
        result_json = caller.execute("search_files", {"query": "budget", "extension": "pdf"})
        assert json.loads(result_json)[0]["name"] == "budget.pdf"

    def test_execute_launch_app(self, tmp_path, mocker):
        startfile = mocker.patch("core.app_indexer.os.startfile")
        caller = ToolCaller(tmp_path / "apps.db", tmp_path / "files.db")
        caller._app_indexer.add_app("Notepad", "C:/Windows/notepad.exe")
        result = json.loads(caller.execute("launch_app", {"app_name": "notepad"}))
        assert result["ok"] is True
        startfile.assert_called_once_with("C:/Windows/notepad.exe")

    def test_execute_read_file(self, tmp_path):
        path = tmp_path / "note.txt"
        path.write_text("hello reader")
        caller = ToolCaller(tmp_path / "apps.db", tmp_path / "files.db")
        result = json.loads(caller.execute("read_file_content", {"file_path": str(path)}))
        assert result["ok"] is True
        assert result["content"] == "hello reader"

    def test_unknown_tool_returns_error(self, tmp_path):
        caller = ToolCaller(tmp_path / "apps.db", tmp_path / "files.db")
        result = json.loads(caller.execute("nope_tool", {}))
        assert result["ok"] is False

    def test_dispatch_builds_tool_messages(self, tmp_path, mocker):
        mocker.patch.object(FileSearch, "search", return_value=[])
        caller = ToolCaller(tmp_path / "apps.db", tmp_path / "files.db")
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_files", "arguments": '{"query": "x"}'},
            }
        ]
        messages = caller.dispatch(calls)
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "call_1"
        assert messages[0]["name"] == "search_files"
        assert json.loads(messages[0]["content"]) == []


# ---------------------------------------------------------------------------
# Agentic pipeline (ai_engine)
# ---------------------------------------------------------------------------

class TestAgenticPipeline:
    def test_local_intent_executes_tool_before_stream(self, tmp_path, qapp, qtbot, mocker):
        bodies = []

        def factory():
            def handler(req):
                payload = json.loads(req.content)
                bodies.append(payload)
                return httpx.Response(200, text=sse_body(openai_chunk("Launched!")))

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        mocker.patch.object(ai_engine, "_build_client", side_effect=factory)
        executed = []

        def fake_execute(self, name, arguments):
            executed.append((name, arguments))
            return json.dumps({"ok": True, "name": arguments.get("app_name"), "path": "C:/x.exe"})

        mocker.patch.object(ToolCaller, "execute", fake_execute)

        cfg = make_config(tmp_path)
        engine = ai_engine.AIEngine(cfg)
        results = []
        engine.done.connect(lambda full, provider: results.append((full, provider)))

        try:
            engine.submit("Launch Notepad")
            qtbot.waitUntil(lambda: len(results) == 1, timeout=5000)
        finally:
            engine.stop()

        assert results[0][0] == "Launched!"
        assert ("launch_app", {"app_name": "Notepad"}) in executed
        tail = bodies[-1]["messages"][-1]["content"]
        assert tail.startswith("[Local tool results]")
        assert "launch_app" in tail

    def test_model_decides_tool_via_function_calling(self, tmp_path, qapp, qtbot, mocker):
        bodies = []

        def handler(req):
            payload = json.loads(req.content)
            bodies.append(payload)
            if payload.get("stream") is False:
                tool_call = {
                    "id": "call_0001",
                    "type": "function",
                    "function": {
                        "name": "search_files",
                        "arguments": '{"query": "budget", "extension": "pdf"}',
                    },
                }
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [tool_call],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                )
            return httpx.Response(200, text=sse_body(openai_chunk("Here is what I found")))

        mocker.patch.object(
            ai_engine,
            "_build_client",
            side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        mocker.patch.object(
            ToolCaller, "search_files",
            return_value='[{"name": "budget.pdf", "path": "C:/docs/budget.pdf"}]',
        )

        cfg = make_config(tmp_path)
        engine = ai_engine.AIEngine(cfg)
        results = []
        engine.done.connect(lambda full, provider: results.append((full, provider)))

        try:
            engine.submit("List every document related to taxes on this machine")
            qtbot.waitUntil(lambda: len(results) == 1, timeout=5000)
        finally:
            engine.stop()

        assert results[0][0] == "Here is what I found"
        assert results[0][1] == "openai"
        # The final stream request must carry the executed tool result.
        final = bodies[-1]
        roles = [m["role"] for m in final["messages"]]
        assert "tool" in roles
        assert final["messages"][-1]["content"] == '[{"name": "budget.pdf", "path": "C:/docs/budget.pdf"}]'

    def test_plain_question_skips_tool_passing(self, tmp_path, qapp, qtbot, mocker):
        bodies = []

        def factory():
            def handler(req):
                payload = json.loads(req.content)
                bodies.append(payload)
                return httpx.Response(200, text=sse_body(openai_chunk("plain answer")))

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        mocker.patch.object(ai_engine, "_build_client", side_effect=factory)

        cfg = make_config(tmp_path)
        engine = ai_engine.AIEngine(cfg)
        results = []
        engine.done.connect(lambda full, provider: results.append((full, provider)))

        try:
            engine.submit("What is the capital of France?")
            qtbot.waitUntil(lambda: len(results) == 1, timeout=5000)
        finally:
            engine.stop()

        assert results[0][0] == "plain answer"
        # exactly one request, no tool round, no tool results injected
        assert len(bodies) == 1
        assert "stream" not in bodies[0] or bodies[0]["stream"] is True