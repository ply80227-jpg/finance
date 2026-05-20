"""Tests for the akshare provider's single-stock CN fast path (P3).

We don't have akshare or pandas installed in CI's lightweight test env, so we
mock the akshare module with a tiny stub that mimics ``stock_bid_ask_em`` and
``stock_zh_a_spot_em``.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_market.providers import akshare_provider


class _FakeRow(dict):  # type: ignore[type-arg]
    def __getitem__(self, key: str) -> Any:  # type: ignore[override]
        return dict.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return dict.get(self, key, default)


class _FakeSeries:
    """A pandas Series-ish thing: equality with a scalar yields a bool mask list."""

    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def __eq__(self, other: Any) -> list[bool]:  # type: ignore[override]
        return [v == other for v in self._values]

    def __ne__(self, other: Any) -> list[bool]:  # type: ignore[override]
        return [v != other for v in self._values]

    def astype(self, _kind: Any) -> _FakeSeries:
        return _FakeSeries([str(v) for v in self._values])

    @property
    def str(self) -> _FakeSeries:
        return self

    def zfill(self, n: int) -> _FakeSeries:
        return _FakeSeries([str(v).zfill(n) for v in self._values])


class _FakeDF:
    """Quacks like the bits of a pandas DataFrame we use."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.empty = not rows

    def __getitem__(self, key: str) -> _FakeSeries:  # type: ignore[override]
        return _FakeSeries([r.get(key) for r in self._rows])

    def iterrows(self):  # type: ignore[no-untyped-def]
        for i, r in enumerate(self._rows):
            yield i, _FakeRow(r)

    def head(self, n: int) -> _FakeDF:
        return _FakeDF(self._rows[:n])

    @property
    def iloc(self):  # type: ignore[no-untyped-def]
        return _ILoc(self._rows)

    @property
    def loc(self):  # type: ignore[no-untyped-def]
        return _Locator(self._rows)


class _ILoc:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __getitem__(self, i: int) -> _FakeRow:
        return _FakeRow(self._rows[i])


class _Locator:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __getitem__(self, mask: list[bool]) -> _FakeDF:  # type: ignore[override]
        return _FakeDF([r for r, keep in zip(self._rows, mask, strict=False) if keep])


@pytest.fixture(autouse=True)
def _reset_spot_cache() -> None:
    akshare_provider._SPOT_CACHE._store.clear()  # type: ignore[attr-defined]


def _bid_ask_df(last: float, change_pct: float, turnover: float, volume: float) -> _FakeDF:
    return _FakeDF(
        [
            {"item": "最新", "value": last},
            {"item": "涨幅", "value": change_pct},
            {"item": "成交额", "value": turnover},
            {"item": "成交量", "value": volume},
            {"item": "今开", "value": 100.0},
            {"item": "最高", "value": 105.0},
            {"item": "最低", "value": 99.0},
            {"item": "昨收", "value": 100.5},
        ]
    )


class _FakeAkBidAsk:
    def __init__(self, df: _FakeDF) -> None:
        self._df = df
        self.calls: list[str] = []

    def stock_bid_ask_em(self, symbol: str) -> _FakeDF:
        self.calls.append(symbol)
        return self._df


def test_quote_cn_uses_stock_bid_ask_em_fast_path() -> None:
    fake_ak = _FakeAkBidAsk(_bid_ask_df(123.45, 1.5, 9_876_543.0, 2_000.0))
    result = akshare_provider.quote_cn(fake_ak, "600519")

    assert result.ok is True
    assert result.provider == "akshare"
    assert result.data["last"] == 123.45
    assert result.data["change_pct"] == 1.5
    assert result.data["turnover"] == 9_876_543.0
    assert result.data["volume"] == 2_000.0
    assert result.data["source"] == "stock_bid_ask_em"
    assert fake_ak.calls == ["600519"]


def test_quote_cn_falls_back_to_spot_when_bid_ask_unavailable() -> None:
    class _AkNoBidAsk:
        # no stock_bid_ask_em attribute
        spot_calls: list[bool] = []

        def stock_zh_a_spot_em(self) -> _FakeDF:
            type(self).spot_calls.append(True)
            return _FakeDF(
                [
                    {
                        "代码": "600519",
                        "名称": "贵州茅台",
                        "最新价": 1700.0,
                        "涨跌幅": 0.8,
                        "成交额": 1_234_567_890.0,
                    }
                ]
            )

    fake_ak = _AkNoBidAsk()
    result = akshare_provider.quote_cn(fake_ak, "600519")

    assert result.ok is True
    assert result.data["last"] == 1700.0
    assert result.data["name"] == "贵州茅台"
    assert result.data.get("source") == "stock_zh_a_spot_em"
    assert _AkNoBidAsk.spot_calls == [True]


def test_quote_cn_falls_back_to_spot_when_bid_ask_returns_no_price() -> None:
    """Some halts / suspensions return an item table with empty 最新."""

    class _Ak:
        def __init__(self) -> None:
            self.bid_calls = 0
            self.spot_calls = 0

        def stock_bid_ask_em(self, symbol: str) -> _FakeDF:
            self.bid_calls += 1
            return _FakeDF([{"item": "最新", "value": ""}])

        def stock_zh_a_spot_em(self) -> _FakeDF:
            self.spot_calls += 1
            return _FakeDF(
                [
                    {
                        "代码": "600519",
                        "名称": "贵州茅台",
                        "最新价": 1700.0,
                        "涨跌幅": 0.8,
                        "成交额": 1_234_567_890.0,
                    }
                ]
            )

    fake_ak = _Ak()
    result = akshare_provider.quote_cn(fake_ak, "600519")

    assert fake_ak.bid_calls == 1
    assert fake_ak.spot_calls == 1
    assert result.ok is True
    assert result.data["last"] == 1700.0
    assert result.data["source"] == "stock_zh_a_spot_em"
