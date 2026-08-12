# OmniSearch AI

A lightweight, Raycast-inspired Windows search overlay powered by AI. Summon it
anywhere with a global hotkey, ask a question, and get an instant streaming
answer from OpenAI, Google Gemini, local Ollama, or any OpenAI-compatible
endpoint — plus free **local system tools** (app launch, file search, file
content reading) that run entirely offline.

Built with **Python 3.12** + **PySide6**. Windows only.

---

## Features

- **Global hotkey overlay** — frameless, always-on-top, cursor-snapped search
  window (`Alt+Space` by default, rebindable from Settings; falls back to
  `Alt+`` ` on hotkey conflict). Compact mode collapses the window when idle.
- **Streaming AI answers** — SSE / NDJSON token streaming via `httpx.AsyncClient`,
  rendered with live Markdown (Ctrl+Scroll zooms the transcript).
- **Multiple providers** — OpenAI-compatible (`/chat/completions`),
  Gemini (`:streamGenerateContent`), Ollama (`/api/chat`, no API key needed),
  plus a custom OpenAI-compatible endpoint. Chain + fallback order configurable.
- **Local system tools (AI function calling)** — no API required:
  - `launch_app` — Start Menu `.lnk` index → fuzzy launch via `os.startfile`.
  - `search_files` — fast indexed search of Desktop/Documents/Downloads/Pictures/
    Music/Videos (<100 ms @ ~20k files).
  - `read_file_content` — text / PDF / DOCX reading with LLM-safe truncation.
  - Deterministic intent detection runs these offline for every provider;
    ambiguous queries let the model decide via an OpenAI `tools` round.
- **Multi-turn conversations** — every query opens/continues a chat in SQLite;
    prior turns are sent as provider context. History list (200 chats), "New
    chat" button.
- **Quick actions** — `/code`, `/explain`, `/sum`, `/web`, `/fix` swap in custom
  system prompts.
- **System tray** — programmatic icon, menu (`Toggle Overlay / Settings… / Quit`),
  single/double click summons the overlay.
- **Single instance** — named mutex; a second launch wakes the running instance.
- **Dark & light themes** — app-wide stylesheets applied from a single config.
- **Secure keys** — API keys are DPAPI-encrypted (current-user only) when entered
  via the Settings dialog; `.env` and OS environment are also supported.

---

## Requirements

- Windows 10/11 (Python scripts use Win32 APIs: DPAPI, RegisterHotKey, named mutex)
- Python 3.12+ (3.11 likely works, untested)
- **One of**:
  - an OpenAI, Gemini, or other OpenAI-compatible API key, **or**
  - a local [Ollama](https://ollama.com) server (fully offline, no key needed),
    e.g. `ollama run qwen2.5:7b`

## Installation

```powershell
git clone <your-repo-url> omnisearch-ai
cd omnisearch-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env  # fill in your keys (or use Settings dialog)
python main.py
```

### What gets installed (`requirements.txt`)

| Package          | Purpose                              |
| ---------------- | ------------------------------------ |
| `PySide6>=6.7`   | Qt GUI: overlay window, tray, settings |
| `httpx>=0.27`    | Async streaming HTTP (SSE / NDJSON)  |
| `pytest>=8` …    | Dev/test only (see Testing below)    |

Everything else ships with the Python standard library — no other runtime
dependencies.

> API keys entered in Settings are stored encrypted (Windows DPAPI). Alternatively
> put them in `.env` or export them as OS environment variables. Lookup order:
> Settings store → `.env` → OS environment.

---

## Usage

1. **Summon** the overlay with the global hotkey (`Alt+Space` by default;
   rebind in Settings). It appears on the screen under your cursor.
2. **Ask** anything — type and press Enter. The answer streams in live Markdown
   (Ctrl+Scroll to zoom). Every query opens or continues a chat inside the
   overlay; the conversation history (up to 200 chats) is listed when the input
   is empty, and "New chat" starts a fresh one.
3. **Local tools are automatic** — no API calls needed for these:
   - `launch Notepad` → launches the app from the Start Menu index.
   - `search for quarterly report` → returns real matching files.
   - `summarize C:\...\notes.pdf` → reads and summarizes the file.
4. **Quick actions** — prefix your prompt with `/code`, `/explain`, `/sum`,
   `/web`, or `/fix` to use a specialised system prompt.
5. **Settings** (tray menu) — provider + model, theme, hotkey capture, API keys,
   auto-start at login.
6. Leave it running in the tray — a second launch finds the live instance
   instead of starting a duplicate.

---

## Configuration

User preferences live in `%APPDATA%\omnisearch-ai\config.json` (registry-free).
Precedence: **config.json → `.env` → built-in defaults**.

| Key                    | Default                    | Meaning                                |
| ---------------------- | -------------------------- | -------------------------------------- |
| `provider`             | `openai`                   | Active provider                        |
| `model` / `base_url`   | *(per provider)*           | Overrides per-provider defaults        |
| `hotkey`               | `Alt+Space`                | Global summon hotkey                   |
| `theme`                | `dark`                     | `dark` or `light`                      |
| `animations`           | `true`                     | Overlay fade animations                |
| `inject_clipboard`     | `true`                     | Pre-fill the search box from clipboard |
| `temperature`          | `0.7`                      | LLM sampling temperature               |
| `max_tokens`           | `2048`                     | Completion token cap                   |
| `history_limit`        | `500`                      | Max stored history rows                |
| `tool_calling`         | `true`                     | Enable AI `tools` round for ambiguous queries |
| `fallback_order`       | `["ollama","openai","gemini"]` | Provider failover chain           |
| `quick_actions`        | `/code /explain /sum /web /fix` | Slash-command system prompts     |
| `auto_start`           | `false`                    | Launch at login                        |

Providers (base URL + model) are defined under `providers.*` in config and the
corresponding `*_BASE_URL` / `*_MODEL` environment keys in `.env.example`.

---

## Project layout

```
main.py                 entry point (DPI → mutex → app → overlay/tray wiring)
config.py               ConfigManager, .env parser, DPAPI SecretsManager
core/
  ai_engine.py          async streaming transports + Qt worker threads; AI tool calling
  app_indexer.py        Start Menu .lnk scan → apps SQLite table + launch_app()
  file_search.py        SQLite-indexed fast file search (in-memory cache)
  file_reader.py        text / PDF (FlateDecode) / DOCX (zip+xml) content reader
  tool_caller.py        LOCAL_TOOLS dispatch: search_files / launch_app / read_file_content
  hotkey.py             Win32 RegisterHotKey + QAbstractNativeEventFilter
  single_instance.py    named-mutex enforcement + wake-broadcast
ui/
  overlay.py            frameless search Overlay window (compact/expanded)
  components.py         SearchBar, StreamingMarkdown (Ctrl+Scroll zoom)
  history.py            SQLite HistoryStore: conversations + messages + legacy flat Q&A
  settings_dialog.py    settings panel + HotkeyCapture key recorder
utils/
  system_info.py        DPI awareness, screen/cursor geometry, memory stats
  logger.py             rotating file + console logging
  win_startup.py        launch-at-login helpers
styles/                 dark_theme.qss / light_theme.qss (per-theme QSS)
tests/                  pytest suite (incl. tests/test_local_system.py)
```

---

## Development

### Running the test suite

```powershell
QT_QPA_PLATFORM=offscreen .\.venv\Scripts\python.exe -m pytest -q
```

The suite runs headless (`QT_QPA_PLATFORM=offscreen`) and currently reports
**168 passed**. It covers config parsing, secrets, the overlay UI, streaming
engine (mock providers), history/multi-turn conversations, and the local system
tools (indexing, search performance, file reading, tool dispatch).

### Startup flow (`main.py`)

1. Per-monitor DPI awareness is claimed **before** any Qt object exists.
2. `SingleInstanceLock` decides primary vs. pinging the live instance.
3. `ConfigManager` is built with the repo `.env` explicitly supplied.
4. The `QSystemTrayIcon` + `GlobalHotkey` native filter track the app lifecycle.
5. A background daemon thread calls `engine.warm_up()` so the local app/file
   indexes are populated and tools return real results on the first query.

### Commit conventions

- Conventional Commits: `feat:`, `fix:`, `refactor:`, `perf:`, `test:`, `docs:`,
  `chore:`.
- Every code change must land with a fully green test suite.

---

## License

This project is released under the **MIT License** — see the
[LICENSE](LICENSE) file for the full text.

In short, you are free to use, copy, modify, merge, publish, distribute,
sublicense, and sell copies of the software, provided the copyright notice and
permission notice are preserved in all copies or substantial portions. The
software is provided "AS IS", without warranty of any kind.

> Note: the project's AI providers (OpenAI, Google Gemini, Ollama) are **not**
> part of this licensed code — they remain governed by their own terms.