"""Tests for the :class:`FetchResult` model and ``fail_result`` helper."""

from __future__ import annotations

from hermes_market.models import SCHEMA_VERSION, FetchResult, fail_result


def test_fetch_result_defaults() -> None:
    r = FetchResult(True, "akshare", "600519", "cn", {"last": 1.0})
    assert r.ok is True
    assert r.error is None
    assert r.errors == []
    assert r.schema_version == SCHEMA_VERSION


def test_fail_result_from_strings_joins_into_legacy_field() -> None:
    r = fail_result("none", "600519", "cn", ["akshare boom", "yfinance kaboom"])
    assert r.ok is False
    assert r.error is not None
    assert "akshare boom" in r.error
    assert "yfinance kaboom" in r.error
    assert len(r.errors) == 2
    assert all("message" in e and "provider" in e for e in r.errors)


def test_fail_result_from_structured() -> None:
    r = fail_result(
        "none",
        "00700",
        "hk",
        [
            {"provider": "akshare", "message": "boom"},
            {"provider": "yfinance", "message": "kaboom"},
        ],
    )
    assert r.errors[0]["provider"] == "akshare"
    assert r.errors[1]["message"] == "kaboom"
