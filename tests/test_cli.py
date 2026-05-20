"""Tests for the CLI entry point — top-level exception handling, JSON shape."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hermes_market import cli
from hermes_market import fetcher as fetcher_mod
from hermes_market.models import FetchResult


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
    assert parsed["schema_version"] == 1
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
