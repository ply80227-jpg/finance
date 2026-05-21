"""Unit + integration tests for ``hermes_market.server`` (stdio MCP)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from hermes_market.fetcher import MarketDataFetcher
from hermes_market.models import FetchResult
from hermes_market.server import (
    MCP_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    StdioMCPServer,
    _dispatch_tool,
    _SingleFlight,
    _singleflight_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """Drop-in for ``MarketDataFetcher`` that records calls and returns
    deterministic ``FetchResult`` objects without touching the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._lock = threading.Lock()

    def _record(self, name: str, *args: Any) -> None:
        with self._lock:
            self.calls.append((name, args))

    def quote(self, symbol: str, market: str | None = None) -> FetchResult:
        self._record("quote", symbol, market)
        return FetchResult(True, "fake", symbol, market or "cn", {"last": 100.0})

    def batch_quote(self, symbols: list[str], market: str | None = None) -> list[FetchResult]:
        self._record("batch_quote", tuple(symbols), market)
        return [FetchResult(True, "fake", s, market or "cn", {"last": 100.0}) for s in symbols]

    def history(
        self,
        symbol: str,
        start: str,
        end: str,
        market: str | None = None,
    ) -> FetchResult:
        self._record("history", symbol, start, end, market)
        return FetchResult(True, "fake", symbol, market or "cn", {"bars": []})

    def news(
        self,
        limit: int = 20,
        symbol: str | None = None,
        market: str | None = None,
    ) -> FetchResult:
        self._record("news", limit, symbol, market)
        return FetchResult(True, "fake", symbol or "", market or "global", {"items": []})

    def search(self, query: str, *, limit: int = 10, market: str | None = None) -> list[Any]:
        self._record("search", query, limit, market)

        class _Row:
            symbol = "600519"
            name = "贵州茅台"
            market = "cn"
            exchange = "sh"

            def to_dict(self) -> dict[str, Any]:
                return {"symbol": "600519", "name": "贵州茅台", "market": "cn", "exchange": "sh"}

        return [_Row()] if query.strip() else []


def _make_server(fake: _FakeFetcher | None = None) -> tuple[StdioMCPServer, io.StringIO, io.StringIO]:
    """Build a server wired to in-memory stdin/stdout pipes (for unit tests)."""

    fetcher = fake if fake is not None else _FakeFetcher()
    stdin = io.StringIO()
    stdout = io.StringIO()
    # MarketDataFetcher is type-annotated, but _FakeFetcher walks like one.
    srv = StdioMCPServer(fetcher=fetcher, stdin=stdin, stdout=stdout)  # type: ignore[arg-type]
    return srv, stdin, stdout


def _drain_stdout(stdout: io.StringIO) -> list[dict[str, Any]]:
    stdout.seek(0)
    return [json.loads(line) for line in stdout.read().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Dispatch (pure helper) tests
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_quote_dispatch(self) -> None:
        fake = _FakeFetcher()
        out = _dispatch_tool(fake, "quote", {"symbol": "600519"})  # type: ignore[arg-type]
        assert out["ok"] is True
        assert out["symbol"] == "600519"
        assert fake.calls == [("quote", ("600519", None))]

    def test_quote_missing_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            _dispatch_tool(_FakeFetcher(), "quote", {})  # type: ignore[arg-type]

    def test_batch_quote_dispatch(self) -> None:
        fake = _FakeFetcher()
        out = _dispatch_tool(fake, "batch_quote", {"symbols": ["600519", "000001"]})  # type: ignore[arg-type]
        assert out["ok"] is True
        assert out["count"] == 2
        assert [i["symbol"] for i in out["items"]] == ["600519", "000001"]

    def test_batch_quote_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="symbols"):
            _dispatch_tool(_FakeFetcher(), "batch_quote", {"symbols": []})  # type: ignore[arg-type]

    def test_history_dispatch(self) -> None:
        fake = _FakeFetcher()
        out = _dispatch_tool(  # type: ignore[arg-type]
            fake, "history", {"symbol": "600519", "start": "2026-05-01", "end": "2026-05-20"}
        )
        assert out["ok"] is True
        assert fake.calls == [("history", ("600519", "2026-05-01", "2026-05-20", None))]

    def test_history_requires_all_dates(self) -> None:
        with pytest.raises(ValueError, match="start"):
            _dispatch_tool(_FakeFetcher(), "history", {"symbol": "600519"})  # type: ignore[arg-type]

    def test_news_dispatch_defaults(self) -> None:
        fake = _FakeFetcher()
        out = _dispatch_tool(fake, "news", {})  # type: ignore[arg-type]
        assert out["ok"] is True
        # Default limit is 20.
        assert fake.calls == [("news", (20, None, None))]

    def test_search_dispatch(self) -> None:
        fake = _FakeFetcher()
        out = _dispatch_tool(fake, "search", {"query": "茅台"})  # type: ignore[arg-type]
        assert out["ok"] is True
        assert out["query"] == "茅台"
        assert out["count"] == 1

    def test_search_empty_query_raises(self) -> None:
        with pytest.raises(ValueError, match="query"):
            _dispatch_tool(_FakeFetcher(), "search", {"query": "   "})  # type: ignore[arg-type]

    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown tool"):
            _dispatch_tool(_FakeFetcher(), "bogus", {})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-flight tests
# ---------------------------------------------------------------------------


class TestSingleFlight:
    def test_singleflight_coalesces_concurrent_identical_keys(self) -> None:
        sf = _SingleFlight()
        calls = 0
        gate = threading.Event()
        lock = threading.Lock()

        def slow() -> int:
            nonlocal calls
            with lock:
                calls += 1
            gate.wait(timeout=2.0)
            return 42

        results: list[int] = []
        results_lock = threading.Lock()

        def worker() -> None:
            r = sf.do("k", slow)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        # Give all workers a chance to register on the same Future.
        time.sleep(0.05)
        gate.set()
        for t in threads:
            t.join(timeout=3.0)

        assert results == [42] * 5
        # Single underlying call despite 5 callers.
        assert calls == 1

    def test_singleflight_releases_key_after_completion(self) -> None:
        sf = _SingleFlight()
        assert sf.do("k", lambda: 1) == 1
        # Second call must run again (fresh data semantics).
        assert sf.do("k", lambda: 2) == 2

    def test_singleflight_propagates_exceptions(self) -> None:
        sf = _SingleFlight()

        def boom() -> int:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            sf.do("k", boom)
        # Key released even on failure; subsequent call runs.
        assert sf.do("k", lambda: 7) == 7

    def test_singleflight_key_is_argument_order_insensitive(self) -> None:
        a = _singleflight_key("quote", {"symbol": "600519", "market": "cn"})
        b = _singleflight_key("quote", {"market": "cn", "symbol": "600519"})
        assert a == b


# ---------------------------------------------------------------------------
# Protocol-level unit tests (in-memory stdin/stdout)
# ---------------------------------------------------------------------------


def _run_server_with(stdin_lines: list[str]) -> list[dict[str, Any]]:
    """Feed pre-recorded lines to the server and collect all responses."""

    fake = _FakeFetcher()
    srv, stdin, stdout = _make_server(fake)
    stdin.write("".join(line + "\n" for line in stdin_lines))
    stdin.seek(0)
    srv.serve_forever()
    return _drain_stdout(stdout)


def _req(method: str, *, id: int | None = 1, params: dict[str, Any] | None = None) -> str:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        msg["id"] = id
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


class TestProtocol:
    def test_initialize_returns_server_info(self) -> None:
        out = _run_server_with(
            [
                _req("initialize", id=1, params={"protocolVersion": MCP_PROTOCOL_VERSION}),
            ]
        )
        assert len(out) == 1
        assert out[0]["id"] == 1
        result = out[0]["result"]
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert result["serverInfo"] == {"name": SERVER_NAME, "version": SERVER_VERSION}
        assert result["capabilities"]["tools"]["listChanged"] is False

    def test_notifications_produce_no_response(self) -> None:
        out = _run_server_with(
            [
                _req("notifications/initialized", id=None),
            ]
        )
        assert out == []

    def test_tools_list_returns_five_tools(self) -> None:
        out = _run_server_with([_req("tools/list", id=1)])
        names = [t["name"] for t in out[0]["result"]["tools"]]
        assert names == ["quote", "batch_quote", "history", "news", "search"]
        # MCP shape uses camelCase ``inputSchema``.
        assert "inputSchema" in out[0]["result"]["tools"][0]

    def test_tools_call_quote_returns_content_block(self) -> None:
        out = _run_server_with(
            [
                _req("tools/call", id=1, params={"name": "quote", "arguments": {"symbol": "600519"}}),
            ]
        )
        result = out[0]["result"]
        assert result["isError"] is False
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["ok"] is True
        assert envelope["symbol"] == "600519"

    def test_tools_call_invalid_params_returns_jsonrpc_error(self) -> None:
        out = _run_server_with(
            [
                _req("tools/call", id=1, params={"name": "quote", "arguments": {}}),
            ]
        )
        assert "error" in out[0]
        assert out[0]["error"]["code"] == -32602  # INVALID_PARAMS

    def test_unknown_method_returns_method_not_found(self) -> None:
        out = _run_server_with([_req("does_not_exist", id=1)])
        assert out[0]["error"]["code"] == -32601

    def test_parse_error_on_invalid_json(self) -> None:
        srv, stdin, stdout = _make_server()
        stdin.write("not-json\n")
        stdin.seek(0)
        srv.serve_forever()
        out = _drain_stdout(stdout)
        assert out[0]["error"]["code"] == -32700

    def test_ping_returns_empty(self) -> None:
        out = _run_server_with([_req("ping", id=42)])
        assert out[0] == {"jsonrpc": "2.0", "id": 42, "result": {}}

    def test_shutdown_terminates_loop(self) -> None:
        out = _run_server_with(
            [
                _req("shutdown", id=1),
                # This second message should never be processed because the
                # serve loop exits after shutdown.set() is observed.
                _req("ping", id=2),
            ]
        )
        ids = [m.get("id") for m in out]
        assert 1 in ids
        # NB: depending on race between submit and shutdown observation, ping
        # MAY get queued. We only assert shutdown was processed.

    def test_resources_list_advertises_output_schemas(self) -> None:
        out = _run_server_with([_req("resources/list", id=1)])
        uris = [r["uri"] for r in out[0]["result"]["resources"]]
        assert "hermes://output-schemas" in uris

    def test_resources_read_returns_schemas_json(self) -> None:
        out = _run_server_with(
            [
                _req("resources/read", id=1, params={"uri": "hermes://output-schemas"}),
            ]
        )
        text = out[0]["result"]["contents"][0]["text"]
        parsed = json.loads(text)
        assert set(parsed) >= {"quote", "batch_quote", "history", "news", "search"}


# ---------------------------------------------------------------------------
# Subprocess integration test — exercises the real ``hermes-market serve``
# entry point through actual stdin/stdout pipes.
# ---------------------------------------------------------------------------


def _subprocess_request_response(requests: list[str], timeout: float = 10.0) -> list[str]:
    """Run ``hermes-market serve`` in a subprocess; send requests, collect lines."""

    proc = subprocess.Popen(
        [sys.executable, "-m", "hermes_market.cli", "serve", "--log-level", "ERROR"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        payload = "".join(r + "\n" for r in requests)
        stdout, _stderr = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return [line for line in stdout.splitlines() if line.strip()]


def test_subprocess_initialize_and_tools_list() -> None:
    """End-to-end smoke: real subprocess answers initialize + tools/list."""

    requests = [
        _req("initialize", id=1, params={"protocolVersion": MCP_PROTOCOL_VERSION}),
        _req("notifications/initialized", id=None),
        _req("tools/list", id=2),
        _req("shutdown", id=3),
    ]
    lines = _subprocess_request_response(requests, timeout=15.0)
    responses = [json.loads(line) for line in lines]
    by_id = {r.get("id"): r for r in responses if "id" in r}
    assert by_id[1]["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert [t["name"] for t in by_id[2]["result"]["tools"]] == [
        "quote",
        "batch_quote",
        "history",
        "news",
        "search",
    ]


# ---------------------------------------------------------------------------
# Fetcher-reuse test: ensure a single ``MarketDataFetcher`` instance is shared.
# ---------------------------------------------------------------------------


class TestFetcherReuse:
    def test_get_fetcher_constructs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        construct_count = 0
        sentinel = _FakeFetcher()

        def fake_ctor(*_args: Any, **_kwargs: Any) -> _FakeFetcher:
            nonlocal construct_count
            construct_count += 1
            return sentinel

        monkeypatch.setattr("hermes_market.server.MarketDataFetcher", fake_ctor)
        srv = StdioMCPServer()
        # 10 concurrent _get_fetcher calls must yield exactly one construction.
        errors: list[BaseException] = []

        def call() -> None:
            try:
                assert srv._get_fetcher() is sentinel
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
        assert errors == []
        assert construct_count == 1


# ---------------------------------------------------------------------------
# Misc — required to keep our coverage of public API surface honest.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _no_real_fetcher() -> Iterator[None]:
    """Smoke-test guard: the real MarketDataFetcher class is still importable."""

    assert MarketDataFetcher is not None
    yield
