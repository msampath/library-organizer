"""Web-search connector for critic evidence — batched, opt-in, offline-safe.

Hard identity calls (a transliterated title, an obscure film) may need a web
lookup. Queries are BATCHED into call-groups to save tokens/requests (§5.2
"grouped to save tokens"), and the whole thing is disabled unless explicitly
enabled — offline returns []. The provider transport is injected (there is no
free keyless search API); tests supply a fake.
"""
from __future__ import annotations

from . import NETWORK_ERRORS

GROUP_SIZE = 5


def batch_queries(queries: list[str], group_size: int = GROUP_SIZE
                  ) -> list[list[str]]:
    """Split queries into call-groups so N lookups cost ceil(N/group_size)
    requests, not N."""
    return [queries[i:i + group_size]
            for i in range(0, len(queries), group_size)]


def search(queries: list[str], *, enabled: bool = False, transport=None,
           group_size: int = GROUP_SIZE, timeout: int = 20) -> list[dict]:
    """Results for each query, or [] when disabled/offline. A transport is
    required when enabled (no keyless default); a failing group is skipped, not
    fatal — a connector never breaks the core."""
    if not enabled or not queries or transport is None:
        return []
    out: list[dict] = []
    for group in batch_queries(queries, group_size):
        try:
            out.extend(transport(group, timeout))
        except NETWORK_ERRORS:
            continue
    return out
