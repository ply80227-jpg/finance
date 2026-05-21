"""Tests for the CLI entry point — top-level exception handling, JSON shape."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hermes_market import cli
from hermes_market import fetcher as fetcher_mod
from hermes_market.models import SCHEMA_VERSION, FetchResult


def test_main_emits_failure_json_on_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrecognized symbol must still produce valid JSON on stdout (P0-4)."""

    # The fetcher constructor must not touch the network even if it's invoked.
    class _DummyClient:
        pass

    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod, "XueqiuClient", _DummyClient)

    rc = cli.main(["quote", "--symbol", "NOT_A_TICKER"])
    out = capsys.readouterr().out.strip()
    parsed: dict[str, Any] = json.loads(out)
    assert rc == 2
    assert parsed["ok"] is False
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["provider"] in {"cli", "none"}


def test_main_emits_success_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class _DummyClient:
        pass

    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", lambda: object())
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod, "XueqiuClient", _DummyClient)

    def _ok_cn(ak, sym):  # type: ignore[no-untyped-def]
        return FetchResult(True, "akshare", sym, "cn", {"last": 100.0, "timestamp": "2026-01-01T00:00:00Z"})

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _ok_cn)

    rc = cli.main(["quote", "--symbol", "600519", "--market", "cn"])
    out = capsys.readouterr().out.strip()
    parsed: dict[str, Any] = json.loads(out)
    assert rc == 0
    assert parsed["ok"] is True
    assert parsed["provider"] == "akshare"
    assert parsed["data"]["last"] == 100.0


def test_tools_subcommand_does_not_touch_providers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``tools`` is a pure-Python introspection command and must not bootstrap providers."""

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("tools subcommand must not import providers")

    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", _explode)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", _explode)

    rc = cli.main(["tools", "--format", "anthropic"])
    out = capsys.readouterr().out.strip()
    parsed: dict[str, Any] = json.loads(out)
    assert rc == 0
    assert parsed["format"] == "anthropic"
    assert {t["name"] for t in parsed["tools"]} >= {"quote", "search", "batch_quote"}


def test_batch_quote_subcommand(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class _DummyClient:
        pass

    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", lambda: object())
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod, "XueqiuClient", _DummyClient)

    def _ok_cn(ak, sym):  # type: ignore[no-untyped-def]
        return FetchResult(True, "akshare", sym, "cn", {"last": 42.0})

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _ok_cn)

    rc = cli.main(["quote", "--symbols", "600519,000001", "--market", "cn"])
    out = capsys.readouterr().out.strip()
    parsed: dict[str, Any] = json.loads(out)
    assert rc == 0
    assert parsed["ok"] is True
    assert parsed["count"] == 2
    assert [it["symbol"] for it in parsed["items"]] == ["600519", "000001"]
    assert all(it["ok"] for it in parsed["items"])


def test_search_subcommand_uses_cached_index(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _DummyClient:
        def _get_json(self, url: str) -> dict[str, Any]:
            return {"data": {"list": []}}

    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "load_module", lambda: None)
    monkeypatch.setattr(fetcher_mod, "XueqiuClient", _DummyClient)

    # Pre-seed the local index loader.
    from hermes_market.symbol_index import SymbolRow

    fake_rows = [
        SymbolRow(symbol="600519", name="贵州茅台", market="cn", exchange="sh"),
        SymbolRow(symbol="00700", name="腾讯控股", market="hk", exchange="hk"),
    ]
    monkeypatch.setattr("hermes_market.fetcher.get_index", lambda _ak=None: fake_rows)

    rc = cli.main(["search", "--query", "茅台"])
    out = capsys.readouterr().out.strip()
    parsed: dict[str, Any] = json.loads(out)
    assert rc == 0
    assert parsed["ok"] is True
    assert parsed["count"] >= 1
    assert parsed["items"][0]["symbol"] == "600519"
