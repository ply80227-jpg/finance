"""Provider implementations.

Each provider is a small module exposing functions that produce
:class:`hermes_market.models.FetchResult` (or raise on failure). The
orchestration / fallback logic lives in :mod:`hermes_market.fetcher`.
"""
