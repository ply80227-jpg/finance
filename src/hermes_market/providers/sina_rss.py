"""Sina financial RSS as a last-resort news fallback source."""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET

from ..models import FetchResult

_RSS_URL = "https://rss.sina.com.cn/finance/allnews.xml"
_DEFAULT_TIMEOUT = 10.0


def news(limit: int, symbol: str | None, timeout: float = _DEFAULT_TIMEOUT) -> FetchResult:
    req = urllib.request.Request(_RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - public RSS feed
        encoding = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read()
    try:
        text = raw.decode(encoding, errors="replace")
        root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError):
        # Fall back to letting ElementTree autodetect from the XML declaration.
        root = ET.fromstring(raw)
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if symbol and symbol not in title:
            continue
        items.append(
            {
                "title": title,
                "time": (item.findtext("pubDate") or "").strip(),
                "source": "sina_rss",
                "url": (item.findtext("link") or "").strip(),
            }
        )
        if len(items) >= limit:
            break
    if not items:
        raise RuntimeError("empty news from sina rss")
    return FetchResult(True, "sina_rss", symbol or "", "global", {"news": items})
