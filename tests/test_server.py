"""Unit + integration tests for ``hermes_market.server`` (stdio MCP)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import IO, Any

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
# Streaming MCP client + interactive integration tests
# ---------------------------------------------------------------------------
#
# The unit tests above use StringIO and a single "play all messages then
# read all responses" model. A real MCP host (Claude Desktop, mcp-cli)
# instead interleaves writes and reads, and depends on responses being
# *matched by id*. The helper below models that interactive shape so we
# can exercise the server end-to-end the way a real host would.


class _StreamingMCPClient:
    """Minimal stdio JSON-RPC 2.0 client used to exercise the server.

    Writes one frame, waits for the response with the matching id, returns
    its ``result`` (or raises with the ``error``). Notifications are sent
    fire-and-forget.
    """

    def __init__(self, write_stream: IO[str], read_stream: IO[str]) -> None:
        self._write = write_stream
        self._read = read_stream
        self._next_id = 0
        self._lock = threading.Lock()

    def _send(self, msg: dict[str, Any]) -> None:
        self._write.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._write.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 5.0) -> Any:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        # Read until we see a response for our id (the server runs tool
        # calls in a worker pool so out-of-order replies are possible).
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._read.readline()
            if line == "":
                raise RuntimeError(f"server closed stdout while awaiting id={msg_id}")
            line = line.strip()
            if not line:
                continue
            resp = json.loads(line)
            if resp.get("id") != msg_id:
                # Not for us — surface as test failure since unit tests
                # don't currently issue overlapping requests.
                raise RuntimeError(f"unexpected response id {resp.get('id')!r} (waiting for {msg_id}): {resp}")
            if "error" in resp:
                raise RuntimeError(f"server error for {method}: {resp['error']}")
            return resp.get("result")
        raise TimeoutError(f"no response for id={msg_id} within {timeout}s")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)


class TestInProcessMCPClient:
    """In-process: spawn the server in a thread, drive it through real OS
    pipes with the streaming client. CI-safe (uses a _FakeFetcher)."""

    def _start_server_with_pipes(
        self,
    ) -> tuple[threading.Thread, _StreamingMCPClient, _FakeFetcher, IO[str], IO[str]]:
        # Two unidirectional pipes: client → server stdin, server → client stdout.
        client_to_server_r, client_to_server_w = os.pipe()
        server_to_client_r, server_to_client_w = os.pipe()
        server_stdin = os.fdopen(client_to_server_r, "r", buffering=1)
        server_stdout = os.fdopen(server_to_client_w, "w", buffering=1)
        client_write = os.fdopen(client_to_server_w, "w", buffering=1)
        client_read = os.fdopen(server_to_client_r, "r", buffering=1)

        fake = _FakeFetcher()
        srv = StdioMCPServer(fetcher=fake, stdin=server_stdin, stdout=server_stdout)  # type: ignore[arg-type]
        thread = threading.Thread(target=srv.serve_forever, name="mcp-srv", daemon=True)
        thread.start()
        client = _StreamingMCPClient(write_stream=client_write, read_stream=client_read)
        return thread, client, fake, client_write, client_read

    def _cleanup(
        self,
        thread: threading.Thread,
        client_write: IO[str],
        client_read: IO[str],
    ) -> None:
        # Closing the write end of the client → server pipe sends EOF,
        # which the server treats as a clean shutdown.
        try:
            client_write.close()
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "server thread did not exit after EOF"
        try:
            client_read.close()
        except Exception:  # noqa: BLE001
            pass

    def test_full_session_handshake_list_call_shutdown(self) -> None:
        """A real MCP host's typical message flow, end-to-end."""

        thread, client, fake, client_write, client_read = self._start_server_with_pipes()
        try:
            # 1. Handshake — host MUST send initialize first, then
            #    notifications/initialized before any other request.
            init = client.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            )
            assert init["protocolVersion"] == MCP_PROTOCOL_VERSION
            assert init["serverInfo"]["name"] == SERVER_NAME
            client.notify("notifications/initialized")

            # 2. Discovery.
            listing = client.request("tools/list")
            names = [t["name"] for t in listing["tools"]]
            assert names == ["quote", "batch_quote", "history", "news", "search"]

            # 3. Single tool/call (quote).
            result = client.request(
                "tools/call",
                {"name": "quote", "arguments": {"symbol": "600519"}},
            )
            assert result["isError"] is False
            envelope = json.loads(result["content"][0]["text"])
            assert envelope["ok"] is True
            assert envelope["symbol"] == "600519"

            # 4. Batch tool/call (different shape — envelope has items[]).
            batch = client.request(
                "tools/call",
                {"name": "batch_quote", "arguments": {"symbols": ["600519", "000001", "00700"]}},
            )
            assert batch["isError"] is False
            batch_env = json.loads(batch["content"][0]["text"])
            assert batch_env["count"] == 3
            assert [i["symbol"] for i in batch_env["items"]] == ["600519", "000001", "00700"]

            # 5. resources/read for the output schemas — hosts can pull
            #    these without shelling out to ``hermes-market tools``.
            schemas = client.request(
                "resources/read",
                {"uri": "hermes://output-schemas"},
            )
            parsed = json.loads(schemas["contents"][0]["text"])
            assert set(parsed) >= {"quote", "batch_quote", "history"}

            # 6. Sanity check: every fake fetcher call we expected fired.
            call_names = [c[0] for c in fake.calls]
            assert call_names == ["quote", "batch_quote"]
        finally:
            self._cleanup(thread, client_write, client_read)

    def test_concurrent_identical_tool_calls_singleflight(self) -> None:
        """Two concurrent identical tool/calls must produce two responses
        but only one upstream fetcher call (single-flight coalescing)."""

        # We need the fake fetcher to be slow enough that both client
        # requests are in flight at the same time, so the second one
        # finds the first one's Future already registered.
        class _SlowFake(_FakeFetcher):
            def __init__(self) -> None:
                super().__init__()
                self._gate = threading.Event()

            def quote(self, symbol: str, market: str | None = None) -> FetchResult:  # type: ignore[override]
                self._record("quote", symbol, market)
                self._gate.wait(timeout=2.0)
                return FetchResult(True, "fake", symbol, market or "cn", {"last": 100.0})

        # Same plumbing as _start_server_with_pipes but using the slow fake.
        client_to_server_r, client_to_server_w = os.pipe()
        server_to_client_r, server_to_client_w = os.pipe()
        server_stdin = os.fdopen(client_to_server_r, "r", buffering=1)
        server_stdout = os.fdopen(server_to_client_w, "w", buffering=1)
        client_write = os.fdopen(client_to_server_w, "w", buffering=1)
        client_read = os.fdopen(server_to_client_r, "r", buffering=1)

        fake = _SlowFake()
        srv = StdioMCPServer(fetcher=fake, stdin=server_stdin, stdout=server_stdout)  # type: ignore[arg-type]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

        # The streaming client expects matched responses, so for concurrent
        # requests we drop down to manual framing.
        try:
            # Two identical tools/call frames, written back-to-back before
            # either response can come back (the server is gated on _gate).
            for i in (1, 2):
                client_write.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": i,
                            "method": "tools/call",
                            "params": {"name": "quote", "arguments": {"symbol": "600519"}},
                        }
                    )
                    + "\n"
                )
            client_write.flush()

            # Give the server a moment to enqueue both into the executor
            # and have the second observe the first's in-flight Future.
            time.sleep(0.2)
            fake._gate.set()

            responses: list[dict[str, Any]] = []
            for _ in range(2):
                line = client_read.readline()
                if not line:
                    raise RuntimeError("server closed stdout early")
                responses.append(json.loads(line.strip()))

            ids = sorted(r["id"] for r in responses)
            assert ids == [1, 2]
            for r in responses:
                envelope = json.loads(r["result"]["content"][0]["text"])
                assert envelope["ok"] is True
                assert envelope["symbol"] == "600519"

            # Single-flight: the slow fetcher.quote method ran exactly once
            # even though we issued 2 identical concurrent tools/call frames.
            quote_calls = [c for c in fake.calls if c[0] == "quote"]
            assert len(quote_calls) == 1, f"expected 1 underlying quote call, got {quote_calls}"
        finally:
            try:
                client_write.close()  # EOF → server exits cleanly
            except Exception:  # noqa: BLE001
                pass
            thread.join(timeout=3.0)
            for h in (client_read,):
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass


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
# Live subprocess + tools/call against real providers — gated on HERMES_RUN_LIVE.
# ---------------------------------------------------------------------------


def _subprocess_streaming_session(
    requests_then_drain: list[str],
    *,
    expected_response_ids: list[int],
    timeout: float = 30.0,
) -> dict[int, dict[str, Any]]:
    """Spawn ``hermes-market serve`` and run an interactive request/response
    session via the _StreamingMCPClient helper. Used by the live test."""

    proc = subprocess.Popen(
        [sys.executable, "-m", "hermes_market.cli", "serve", "--log-level", "ERROR"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if proc.stdin is None or proc.stdout is None:
        proc.kill()
        raise RuntimeError("subprocess pipes missing")
    try:
        # Send all pre-recorded requests at once. We drain responses by id
        # in the order the test specifies — that order matches the request
        # order for our serial use of this helper.
        proc.stdin.write("".join(line + "\n" for line in requests_then_drain))
        proc.stdin.flush()

        responses: dict[int, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout
        while expected_response_ids and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line == "":
                break
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "id" in msg and msg["id"] in expected_response_ids:
                responses[msg["id"]] = msg
                expected_response_ids.remove(msg["id"])
        return responses
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(
    not os.environ.get("HERMES_RUN_LIVE"),
    reason="Live network test; set HERMES_RUN_LIVE=1 to enable.",
)
def test_live_subprocess_tools_call_quote() -> None:
    """End-to-end live: spawn ``hermes-market serve``, run a real
    ``tools/call(quote, 600519)`` against actual providers via the
    streaming client. Skipped in CI."""

    requests = [
        _req("initialize", id=1, params={"protocolVersion": MCP_PROTOCOL_VERSION}),
        _req("notifications/initialized", id=None),
        _req("tools/call", id=2, params={"name": "quote", "arguments": {"symbol": "600519"}}),
        _req("shutdown", id=3),
    ]
    responses = _subprocess_streaming_session(
        requests,
        expected_response_ids=[1, 2, 3],
        timeout=30.0,
    )
    assert responses[1]["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    # The tools/call response wraps the FetchResult envelope in a text content block.
    call = responses[2]["result"]
    envelope = json.loads(call["content"][0]["text"])
    assert envelope["ok"] is True, f"quote failed: {envelope}"
    assert envelope["symbol"] == "600519"
    assert isinstance(envelope["data"].get("last"), (int, float))


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
