"""Unit tests for the rewritten stooq provider (direct HTTP CSV)."""

from __future__ import annotations

import pytest

from hermes_market.providers import stooq_provider


@pytest.fixture
def patch_http(monkeypatch: pytest.MonkeyPatch):
    """Replace ``stooq_provider._http_get`` with an in-memory stub.

    Returns a closure that records every URL/params pair and is the
    canonical way to assert on outbound calls.
    """

    calls: list[tuple[str, dict[str, str]]] = []
    bodies: dict[str, str] = {}

    def _fake(url: str, params: dict[str, str], *, timeout: float = 6.0) -> str:
        calls.append((url, params))
        # Return whichever body the test pre-loaded for this URL bucket.
        key = "history" if url.endswith("/q/d/l/") else "quote"
        return bodies.get(key, "")

    monkeypatch.setattr(stooq_provider, "_http_get", _fake)

    def configure(*, quote: str | None = None, history: str | None = None) -> list[tuple[str, dict[str, str]]]:
        if quote is not None:
            bodies["quote"] = quote
        if history is not None:
            bodies["history"] = history
        return calls

    return configure


class TestQuote:
    """The CN/HK latest-quote path."""

    def test_parses_a_share_csv(self, patch_http) -> None:
        calls = patch_http(
            quote=(
                "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                "600519.CN,2026-05-19,08:57:00,1321.9,1329.98,1318,1324.3,4325464\n"
            )
        )
        result = stooq_provider.quote(None, "600519", "cn")
        assert result.ok is True
        assert result.provider == "stooq"
        assert result.data is not None
        assert result.data["last"] == 1324.3
        assert result.data["volume"] == 4325464
        assert result.data["as_of"] == "2026-05-19"
        # Open=1321.9, Close=1324.3 → +0.18%
        assert result.data["change_pct"] == pytest.approx(0.1815, abs=1e-3)
        # Outbound URL must use the Stooq CN suffix, not the Yahoo .SS suffix.
        assert calls[0][1]["s"] == "600519.cn"

    def test_uses_hk_short_form(self, patch_http) -> None:
        calls = patch_http(
            quote=(
                "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                "700.HK,2026-05-19,10:00:00,449.2,468.8,448.6,460,33736701\n"
            )
        )
        result = stooq_provider.quote(None, "00700", "hk")
        assert result.ok is True
        assert result.data is not None
        assert result.data["last"] == 460.0
        # Critical regression: Stooq HK uses bare digits, not zero-padded.
        assert calls[0][1]["s"] == "700.hk"

    def test_nd_raises(self, patch_http) -> None:
        patch_http(quote=("Symbol,Date,Time,Open,High,Low,Close,Volume\n0700.HK,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"))
        with pytest.raises(ValueError, match="N/D|stooq has no data"):
            stooq_provider.quote(None, "0700", "hk")

    def test_empty_body_raises(self, patch_http) -> None:
        patch_http(quote="")
        with pytest.raises(ValueError, match="empty quote"):
            stooq_provider.quote(None, "600519", "cn")


class TestHistory:
    def test_parses_csv_rows(self, patch_http) -> None:
        calls = patch_http(
            history=(
                "Date,Open,High,Low,Close,Volume\n"
                "2026-05-15,1300.0,1310.0,1295.0,1305.5,2000000\n"
                "2026-05-16,1305.0,1325.0,1304.0,1322.1,3100000\n"
            )
        )
        result = stooq_provider.history(None, "600519", "cn", "20260515", "20260516")
        assert result.ok is True
        assert result.data is not None
        bars = result.data["bars"]
        assert len(bars) == 2
        assert bars[0]["date"] == "2026-05-15"
        assert bars[1]["close"] == 1322.1
        # Date params propagate verbatim when already YYYYMMDD.
        assert calls[0][1]["d1"] == "20260515"
        assert calls[0][1]["d2"] == "20260516"

    def test_accepts_iso_dates(self, patch_http) -> None:
        calls = patch_http(history="Date,Open,High,Low,Close,Volume\n2026-05-15,1.0,1.0,1.0,1.0,1\n")
        stooq_provider.history(None, "600519", "cn", "2026-05-15", "2026-05-16")
        assert calls[0][1]["d1"] == "20260515"
        assert calls[0][1]["d2"] == "20260516"

    def test_apikey_gate_raises(self, patch_http) -> None:
        patch_http(
            history=(
                "Get your apikey:\n\n"
                "1. Open https://stooq.com/q/d/?s=600519.cn&get_apikey\n"
                "2. Enter the captcha code.\n"
            )
        )
        with pytest.raises(RuntimeError, match="apikey"):
            stooq_provider.history(None, "600519", "cn", "20260101", "20260201")

    def test_apikey_env_appends_param(self, patch_http, monkeypatch) -> None:
        monkeypatch.setenv("STOOQ_APIKEY", "test-key-123")
        calls = patch_http(history="Date,Open,High,Low,Close,Volume\n2026-05-15,1.0,1.0,1.0,1.0,1\n")
        stooq_provider.history(None, "600519", "cn", "20260101", "20260201")
        assert calls[0][1].get("apikey") == "test-key-123"


class TestLoadModule:
    def test_returns_non_none_sentinel(self) -> None:
        # The runner short-circuits when ``load_module`` returns ``None``; with
        # direct HTTP we no longer need a third-party module, but the sentinel
        # must remain non-None so the chain still attempts stooq.
        assert stooq_provider.load_module() is not None


class TestApikeyGateDetection:
    @pytest.mark.parametrize(
        "body",
        [
            "Get your apikey:\n",
            "  get your apikey:\n",
            "Some preamble\nget_apikey=foo\n",
        ],
    )
    def test_detected(self, body: str) -> None:
        assert stooq_provider._is_apikey_gate(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "Date,Open,High,Low,Close,Volume\n2026-05-15,1,1,1,1,1\n",
            "",
            "Symbol,Date,Time,Open,High,Low,Close,Volume\n600519.CN,...\n",
        ],
    )
    def test_not_detected(self, body: str) -> None:
        assert stooq_provider._is_apikey_gate(body) is False
