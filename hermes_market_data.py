#!/usr/bin/env python3
"""Backwards-compatible CLI shim.

The historical README invocation ``python hermes_market_data.py ...`` keeps
working: this file just forwards to the packaged entry point in
:mod:`hermes_market.cli`. New code should depend on the ``hermes_market``
package directly (installed via ``pip install -e .``) and call
``hermes_market.cli:main`` or the ``hermes-market`` console script.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_market.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
