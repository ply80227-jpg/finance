"""Hermes free A-share / HK-share market data package.

This package was extracted from the original single-file ``hermes_market_data.py``
script. The public CLI entry point is :func:`hermes_market.cli.main`. A thin shim
script at the repository root (``hermes_market_data.py``) re-exports it so the
historical invocation ``python hermes_market_data.py ...`` keeps working.
"""

from .models import FetchResult, fail_result

__all__ = ["FetchResult", "fail_result"]
