"""Verification tests for the SQLite history store."""

from ui.history import HistoryStore


def make_store(tmp_path, limit=500):
    return HistoryStore(tmp_path / "history.db", limit=limit)


class TestHistoryStore:
    def test_add_and_count(self, tmp_path):
        store = make_store(tmp_path)
        store.add("what is qt", "Qt is a toolkit.", "openai")
        assert store.count() == 1
        store.close()

    def test_recent_newest_first(self, tmp_path):
        store = make_store(tmp_path)
        for i in range(3):
            store.add(f"query {i}", "ans", "ollama")
        rows = store.recent()
        assert [r["query"] for r in rows] == ["query 2", "query 1", "query 0"]
        store.close()

    def test_response_roundtrip(self, tmp_path):
        store = make_store(tmp_path)
        rid = store.add("sum nums", "42", "gemini")
        assert store.response_for(rid) == "42"
        store.close()

    def test_fuzzy_search_case_insensitive(self, tmp_path):
        store = make_store(tmp_path)
        store.add("Show me KNMI forecasts", "a", "openai")
        store.add("knit a sweater", "b", "openai")
        store.add("unrelated", "c", "openai")
        hits = store.search("kn", limit=10)
        # Prefix matches rank first, then recency.
        assert [r["query"] for r in hits] == ["knit a sweater", "Show me KNMI forecasts"]
        store.close()

    def test_empty_needle_returns_recent(self, tmp_path):
        store = make_store(tmp_path)
        store.add("foo", "a", "openai")
        assert len(store.search("")) == 1
        store.close()

    def test_trim_respects_limit(self, tmp_path):
        store = make_store(tmp_path, limit=10)
        for i in range(25):
            store.add(f"q{i}", "a", "openai")
        assert store.count() == 10
        assert store.recent(100)[0]["query"] == "q24"
        store.close()

    def test_clear(self, tmp_path):
        store = make_store(tmp_path)
        store.add("x", "y", "openai")
        store.clear()
        assert store.count() == 0
        store.close()

    def test_empty_query_not_stored(self, tmp_path):
        store = make_store(tmp_path)
        assert store.add("   ", "resp", "openai") == 0
        assert store.count() == 0
        store.close()

    def test_close_is_idempotent(self, tmp_path):
        store = make_store(tmp_path)
        store.close()
        store.close()